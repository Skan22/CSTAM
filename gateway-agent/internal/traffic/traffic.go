// Package traffic turns Traefik's JSON access log into per-host counters the control plane can
// show: how many requests each team's host got, how many failed, how many bytes and how slowly.
//
// Only what a dashboard needs is kept: no client addresses, no query strings, no headers.
package traffic

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"os"
	"strings"
	"sync"
	"time"
)

// MaxPath bounds the request path kept for the recent-requests list.
const MaxPath = 200

// Entry is one request.
type Entry struct {
	At       time.Time
	Host     string
	Method   string
	Path     string
	Status   int
	Duration time.Duration
	Bytes    int64
}

type logLine struct {
	Host     string `json:"RequestHost"`
	Method   string `json:"RequestMethod"`
	Path     string `json:"RequestPath"`
	Status   int    `json:"DownstreamStatus"`
	Duration int64  `json:"Duration"`
	Bytes    int64  `json:"DownstreamContentSize"`
	Router   string `json:"RouterName"`
	Start    string `json:"StartUTC"`
}

// Parse reads one access-log line. It refuses lines that are not a customer request: malformed
// ones, ones without a host, and the agent's own canary probes.
func Parse(line []byte) (Entry, bool) {
	var l logLine
	if err := json.Unmarshal(line, &l); err != nil || l.Host == "" {
		return Entry{}, false
	}
	if strings.HasPrefix(l.Router, "ipo-canary") {
		return Entry{}, false
	}
	at, err := time.Parse(time.RFC3339Nano, l.Start)
	if err != nil {
		at = time.Now().UTC()
	}
	path := l.Path
	if i := strings.IndexByte(path, '?'); i >= 0 {
		path = path[:i]
	}
	if len(path) > MaxPath {
		path = path[:MaxPath]
	}
	return Entry{At: at, Host: strings.ToLower(l.Host), Method: l.Method, Path: path,
		Status: l.Status, Duration: time.Duration(l.Duration), Bytes: l.Bytes}, true
}

// HostStats are one host's counters over a flush interval.
type HostStats struct {
	Host       string `json:"host"`
	Requests   int64  `json:"requests"`
	S2xx       int64  `json:"s2xx"`
	S3xx       int64  `json:"s3xx"`
	S4xx       int64  `json:"s4xx"`
	S5xx       int64  `json:"s5xx"`
	Bytes      int64  `json:"bytes"`
	DurationMS int64  `json:"duration_ms_sum"`
}

// Recent is one request kept for the recent-requests list.
type Recent struct {
	At         time.Time `json:"at"`
	Host       string    `json:"host"`
	Method     string    `json:"method"`
	Path       string    `json:"path"`
	Status     int       `json:"status"`
	DurationMS int64     `json:"duration_ms"`
}

// Batch is what one flush sends.
type Batch struct {
	Hosts   []HostStats `json:"hosts"`
	Recent  []Recent    `json:"recent"`
	Dropped int64       `json:"dropped"`
}

// Aggregator accumulates entries between flushes. It is bounded: past maxHosts distinct hosts,
// new ones are counted as dropped, and only the newest maxRecent requests are kept.
type Aggregator struct {
	mu        sync.Mutex
	maxHosts  int
	maxRecent int
	hosts     map[string]*HostStats
	recent    []Recent
	dropped   int64
}

func NewAggregator(maxHosts, maxRecent int) *Aggregator {
	return &Aggregator{maxHosts: maxHosts, maxRecent: maxRecent, hosts: map[string]*HostStats{}}
}

func (a *Aggregator) stats(host string) *HostStats {
	h := a.hosts[host]
	if h == nil {
		if len(a.hosts) >= a.maxHosts {
			return nil
		}
		h = &HostStats{Host: host}
		a.hosts[host] = h
	}
	return h
}

func (a *Aggregator) Add(e Entry) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.recent = append(a.recent, Recent{At: e.At, Host: e.Host, Method: e.Method, Path: e.Path,
		Status: e.Status, DurationMS: e.Duration.Milliseconds()})
	if over := len(a.recent) - a.maxRecent; over > 0 {
		a.recent = append([]Recent(nil), a.recent[over:]...)
	}
	h := a.stats(e.Host)
	if h == nil {
		a.dropped++
		return
	}
	h.Requests++
	switch {
	case e.Status >= 500:
		h.S5xx++
	case e.Status >= 400:
		h.S4xx++
	case e.Status >= 300:
		h.S3xx++
	default:
		h.S2xx++
	}
	h.Bytes += e.Bytes
	h.DurationMS += e.Duration.Milliseconds()
}

// Flush returns what has accumulated and starts over.
func (a *Aggregator) Flush() Batch {
	a.mu.Lock()
	defer a.mu.Unlock()
	b := Batch{Recent: a.recent, Dropped: a.dropped, Hosts: make([]HostStats, 0, len(a.hosts))}
	if b.Recent == nil {
		b.Recent = []Recent{} // the API wants a list, not null
	}
	for _, h := range a.hosts {
		b.Hosts = append(b.Hosts, *h)
	}
	a.hosts, a.recent, a.dropped = map[string]*HostStats{}, nil, 0
	return b
}

// Requeue puts a batch that could not be delivered back, so the counts are sent next time.
func (a *Aggregator) Requeue(b Batch) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, s := range b.Hosts {
		h := a.stats(s.Host)
		if h == nil {
			a.dropped += s.Requests
			continue
		}
		h.Requests += s.Requests
		h.S2xx += s.S2xx
		h.S3xx += s.S3xx
		h.S4xx += s.S4xx
		h.S5xx += s.S5xx
		h.Bytes += s.Bytes
		h.DurationMS += s.DurationMS
	}
	a.dropped += b.Dropped
	a.recent = append(append([]Recent(nil), b.Recent...), a.recent...)
	if over := len(a.recent) - a.maxRecent; over > 0 {
		a.recent = a.recent[over:]
	}
}

// Tail follows the file, calling line for each complete line written after it started. History
// is skipped when the file already exists at start (it was counted or is not wanted); a file that
// appears later, is rotated, or is truncated is read from its beginning. It returns when ctx ends.
func Tail(ctx context.Context, path string, every time.Duration, line func([]byte)) {
	var (
		f       *os.File
		offset  int64
		pending []byte
		first   = true
	)
	defer func() {
		if f != nil {
			f.Close()
		}
	}()
	open := func() {
		nf, err := os.Open(path)
		if err != nil {
			return
		}
		if f != nil {
			f.Close()
		}
		f, pending, offset = nf, nil, 0
		if first {
			offset, _ = nf.Seek(0, io.SeekEnd)
		}
		first = false
	}
	t := time.NewTicker(every)
	defer t.Stop()
	buf := make([]byte, 64<<10)
	for {
		if f == nil {
			open()
			if f == nil {
				first = false // a file that shows up after we started is all new
			}
		} else if cur, err := os.Stat(path); err != nil {
			// gone: keep draining the old handle below, then reopen once the new file exists
		} else if st, err := f.Stat(); err == nil && !os.SameFile(cur, st) {
			drain(f, &offset, &pending, buf, line)
			open()
		} else if err == nil && cur.Size() < offset {
			offset, pending = 0, nil
			_, _ = f.Seek(0, io.SeekStart)
		}
		if f != nil {
			drain(f, &offset, &pending, buf, line)
		}
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

func drain(f *os.File, offset *int64, pending *[]byte, buf []byte, line func([]byte)) {
	for {
		n, err := f.ReadAt(buf, *offset)
		if n > 0 {
			*offset += int64(n)
			*pending = append(*pending, buf[:n]...)
			for {
				i := bytes.IndexByte(*pending, '\n')
				if i < 0 {
					break
				}
				if i > 0 {
					line((*pending)[:i])
				}
				*pending = (*pending)[i+1:]
			}
		}
		if err != nil || n == 0 {
			return
		}
	}
}

// Reporter tails the access log and sends the counters every Every. A batch that could not be
// delivered is folded into the next one, so a control-plane outage loses no counts.
type Reporter struct {
	Path  string
	Every time.Duration
	Poll  time.Duration
	Send  func(context.Context, Batch) error
	Agg   *Aggregator
	Logf  func(format string, args ...any)
}

func (r *Reporter) Run(ctx context.Context) {
	go Tail(ctx, r.Path, r.Poll, func(l []byte) {
		if e, ok := Parse(l); ok {
			r.Agg.Add(e)
		}
	})
	t := time.NewTicker(r.Every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
		b := r.Agg.Flush()
		if len(b.Hosts) == 0 && len(b.Recent) == 0 && b.Dropped == 0 {
			continue
		}
		if err := r.Send(ctx, b); err != nil {
			r.Agg.Requeue(b)
			if r.Logf != nil {
				r.Logf("traffic report failed: %v", err)
			}
		}
	}
}

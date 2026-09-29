package traffic

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// A line as the real Traefik 3 writes it (checked against the lab's access log).
const realLine = `{"ClientAddr":"10.0.0.254:40526","ClientHost":"10.0.0.254","DownstreamContentSize":54,"DownstreamStatus":200,"Duration":927946,"OriginStatus":200,"RequestAddr":"chaos.cstam.felcloud.tn","RequestHost":"chaos.cstam.felcloud.tn","RequestMethod":"GET","RequestPath":"/hello","RequestScheme":"http","RouterName":"team-chaos@file","ServiceName":"team-chaos@file","StartUTC":"2026-09-29T20:44:56.825857408Z","entryPointName":"web","level":"info","msg":"","time":"2026-09-29T21:44:56+01:00"}`

func TestParseReadsWhatTheDashboardNeeds(t *testing.T) {
	e, ok := Parse([]byte(realLine))
	if !ok {
		t.Fatal("a real access-log line was not parsed")
	}
	if e.Host != "chaos.cstam.felcloud.tn" || e.Method != "GET" || e.Path != "/hello" ||
		e.Status != 200 || e.Bytes != 54 {
		t.Fatalf("wrong fields: %+v", e)
	}
	if e.Duration != 927946*time.Nanosecond {
		t.Fatalf("duration %v", e.Duration)
	}
	if e.At.UTC().Format(time.RFC3339) != "2026-09-29T20:44:56Z" {
		t.Fatalf("time %v", e.At)
	}
}

func TestParseIgnoresWhatIsNotACustomerRequest(t *testing.T) {
	canary := strings.Replace(realLine, "team-chaos@file", "ipo-canary@file", 1)
	for name, line := range map[string]string{
		"garbage":       "not json",
		"no host":       `{"DownstreamStatus":200}`,
		"canary probes": canary,
		"empty":         "",
	} {
		if _, ok := Parse([]byte(line)); ok {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestParseDropsTheQueryAndBoundsThePath(t *testing.T) {
	line := strings.Replace(realLine, `"/hello"`, `"/a?token=secret"`, 1)
	e, _ := Parse([]byte(line))
	if e.Path != "/a" {
		t.Fatalf("query string kept: %q", e.Path)
	}
	long := strings.Replace(realLine, `"/hello"`, `"/`+strings.Repeat("x", 500)+`"`, 1)
	e, _ = Parse([]byte(long))
	if len(e.Path) > MaxPath {
		t.Fatalf("path not bounded: %d", len(e.Path))
	}
}

func entry(host string, status int, ms int, bytes int64) Entry {
	return Entry{At: time.Unix(1000, 0), Host: host, Method: "GET", Path: "/", Status: status,
		Duration: time.Duration(ms) * time.Millisecond, Bytes: bytes}
}

func TestAggregatorCountsPerHostAndClass(t *testing.T) {
	a := NewAggregator(10, 3)
	a.Add(entry("a.x", 200, 10, 100))
	a.Add(entry("a.x", 404, 30, 50))
	a.Add(entry("a.x", 503, 20, 0))
	a.Add(entry("b.x", 301, 5, 10))
	b := a.Flush()
	got := map[string]HostStats{}
	for _, h := range b.Hosts {
		got[h.Host] = h
	}
	x := got["a.x"]
	if x.Requests != 3 || x.S2xx != 1 || x.S4xx != 1 || x.S5xx != 1 || x.Bytes != 150 || x.DurationMS != 60 {
		t.Fatalf("a.x: %+v", x)
	}
	if got["b.x"].S3xx != 1 {
		t.Fatalf("b.x: %+v", got["b.x"])
	}
	if len(a.Flush().Hosts) != 0 {
		t.Fatal("a flush must reset the counters")
	}
}

func TestAggregatorBoundsWhatItKeeps(t *testing.T) {
	a := NewAggregator(2, 3)
	for i := 0; i < 5; i++ {
		a.Add(entry("h"+string(rune('a'+i))+".x", 200, 1, 1))
	}
	b := a.Flush()
	if len(b.Hosts) != 2 || b.Dropped != 3 {
		t.Fatalf("hosts %d dropped %d", len(b.Hosts), b.Dropped)
	}
	if len(b.Recent) != 3 {
		t.Fatalf("recent %d, want the newest 3", len(b.Recent))
	}
	if b.Recent[2].Host != "he.x" {
		t.Fatalf("newest recent is %s", b.Recent[2].Host)
	}
}

func TestRequeueAddsAFailedBatchBack(t *testing.T) {
	a := NewAggregator(10, 3)
	a.Add(entry("a.x", 200, 10, 100))
	b := a.Flush()
	a.Add(entry("a.x", 500, 10, 1))
	a.Requeue(b)
	got := a.Flush().Hosts[0]
	if got.Requests != 2 || got.S2xx != 1 || got.S5xx != 1 {
		t.Fatalf("%+v", got)
	}
}

func appendTo(t *testing.T, path, s string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.WriteString(s); err != nil {
		t.Fatal(err)
	}
}

type sink struct {
	mu  sync.Mutex
	got []string
}

func (s *sink) add(line []byte) { s.mu.Lock(); s.got = append(s.got, string(line)); s.mu.Unlock() }
func (s *sink) lines() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.got...)
}

func eventually(t *testing.T, what string, f func() bool) {
	t.Helper()
	for i := 0; i < 200; i++ {
		if f() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func TestTailFollowsAppendsAndSkipsHistory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "access.log")
	appendTo(t, path, "old\n")
	s := &sink{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go Tail(ctx, path, 5*time.Millisecond, s.add)
	time.Sleep(50 * time.Millisecond)
	appendTo(t, path, "one\ntw")
	time.Sleep(30 * time.Millisecond)
	appendTo(t, path, "o\n")
	eventually(t, "both lines", func() bool { return len(s.lines()) == 2 })
	if l := s.lines(); l[0] != "one" || l[1] != "two" {
		t.Fatalf("got %q (a partial line must wait for its newline, history must be skipped)", l)
	}
}

func TestTailSurvivesRotationAndAFileThatAppearsLate(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "access.log")
	s := &sink{}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go Tail(ctx, path, 5*time.Millisecond, s.add)
	time.Sleep(30 * time.Millisecond)
	appendTo(t, path, "first\n")
	eventually(t, "first", func() bool { return len(s.lines()) == 1 })
	if err := os.Rename(path, path+".1"); err != nil {
		t.Fatal(err)
	}
	appendTo(t, path, "second\n")
	eventually(t, "second after rotation", func() bool { return len(s.lines()) == 2 })
	if err := os.Truncate(path, 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Millisecond)
	appendTo(t, path, "third\n")
	eventually(t, "third after truncation", func() bool { return len(s.lines()) == 3 })
}

func TestReporterSendsCountsAndRetriesAfterAFailure(t *testing.T) {
	path := filepath.Join(t.TempDir(), "access.log")
	appendTo(t, path, "")
	var mu sync.Mutex
	var sent []Batch
	fail := true
	send := func(_ context.Context, b Batch) error {
		mu.Lock()
		defer mu.Unlock()
		if fail {
			fail = false
			return context.DeadlineExceeded
		}
		sent = append(sent, b)
		return nil
	}
	r := &Reporter{Path: path, Every: 20 * time.Millisecond, Poll: 5 * time.Millisecond, Send: send,
		Agg: NewAggregator(10, 5)}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go r.Run(ctx)
	time.Sleep(30 * time.Millisecond)
	appendTo(t, path, realLine+"\n"+realLine+"\n")
	eventually(t, "the two requests to arrive despite one failed send", func() bool {
		mu.Lock()
		defer mu.Unlock()
		var n int64
		for _, b := range sent {
			for _, h := range b.Hosts {
				n += h.Requests
			}
		}
		return n == 2
	})
}

func TestAnEmptyFlushEncodesListsNotNull(t *testing.T) {
	raw, _ := json.Marshal(NewAggregator(1, 1).Flush())
	if string(raw) != `{"hosts":[],"recent":[],"dropped":0}` {
		t.Fatal(string(raw))
	}
}

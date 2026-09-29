// Package pipeline applies a signed config version to Traefik, or leaves the gateway exactly as
// it was.
//
// Stages, each traced: verify (signature, hash, replay), validate (policy), stage (write and
// fsync a private file), swap (atomic rename over the live file Traefik watches), converge (wait
// until Traefik reports the expected routers), probe (canary and every changed route), commit
// (record the new last-known-good). Any failure after the swap restores the last-known-good by
// renaming a hard link back over the live file, which needs neither disk space nor a copy.
//
// Crash safety comes from the ordering, not from a journal. The committed version is the file
// lkg-<version>.json; it appears by one atomic rename, and on start the live file is always reset
// to the newest one. A process killed at any earlier point therefore comes back serving the
// previous verified config.
package pipeline

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/metrics"
	"github.com/felcloud/ipo/gateway-agent/internal/policy"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/trace"
)

// EmptyConfig is what a gateway serves before it has ever received a config.
const EmptyConfig = `{"http":{}}`

// CanaryRouter is the router name the compiler always emits.
const CanaryRouter = "ipo-canary"

// Kind classifies why an Apply did not succeed.
type Kind string

const (
	KindSignature Kind = "signature" // not signed by the control plane
	KindReplay    Kind = "replay"    // not newer than what is live
	KindInvalid   Kind = "invalid"   // fails policy
	KindStorage   Kind = "storage"   // could not be written; the live config is untouched
	KindRollback  Kind = "rollback"  // applied, failed verification and was rolled back
)

// Rejection is the error returned for every non-success outcome.
type Rejection struct {
	Kind Kind
	Err  error
}

func (r *Rejection) Error() string { return string(r.Kind) + ": " + r.Err.Error() }
func (r *Rejection) Unwrap() error { return r.Err }

// ErrCrashed is returned when the CrashAt test hook simulates the process being killed.
var ErrCrashed = errors.New("simulated crash")

// Traefik reports which file-provider routers it is serving.
type Traefik interface {
	Routers(ctx context.Context) ([]string, error)
}

// Prober requests a host through Traefik and returns an error unless it is routed and its
// backend answers.
type Prober interface {
	Probe(ctx context.Context, host string) error
}

// Config wires a Pipeline.
type Config struct {
	Dir     string
	Key     ed25519.PublicKey
	Rules   policy.Rules
	Traefik Traefik
	Prober  Prober
	Tracer  *trace.Tracer
	Metrics *metrics.Registry

	ConvergeTimeout time.Duration
	ProbeBudget     time.Duration
	PollInterval    time.Duration

	WriteFile func(path string, data []byte) error // default WriteSynced; a test seam for a full disk
	CrashAt   func(point string) bool              // test seam: simulate a kill at "staged", "swapped", "verified", "committed"
}

// Pipeline is safe for concurrent use; applies are serialised.
type Pipeline struct {
	cfg  Config
	mu   sync.Mutex
	ver  int64
	sha  string
	live *policy.Config
}

var lkgName = regexp.MustCompile(`^lkg-(\d+)\.json$`)

// New recovers the last-known-good and resets the live file to it.
func New(cfg Config) (*Pipeline, error) {
	if cfg.WriteFile == nil {
		cfg.WriteFile = WriteSynced
	}
	if cfg.Tracer == nil {
		cfg.Tracer = trace.New(nil, false)
	}
	if cfg.Metrics == nil {
		cfg.Metrics = metrics.New()
	}
	if cfg.PollInterval == 0 {
		cfg.PollInterval = 20 * time.Millisecond
	}
	if cfg.ConvergeTimeout == 0 {
		cfg.ConvergeTimeout = 10 * time.Second
	}
	if cfg.ProbeBudget == 0 {
		cfg.ProbeBudget = 3 * time.Second
	}
	p := &Pipeline{cfg: cfg}
	if err := os.MkdirAll(cfg.Dir, 0o755); err != nil {
		return nil, err
	}
	if err := p.recover(); err != nil {
		return nil, err
	}
	cfg.Metrics.SetVersion(p.ver)
	return p, nil
}

func (p *Pipeline) path(name string) string { return filepath.Join(p.cfg.Dir, name) }

// newestLKG returns the highest committed version and its file, or 0.
func (p *Pipeline) newestLKG() (int64, string, error) {
	entries, err := os.ReadDir(p.cfg.Dir)
	if err != nil {
		return 0, "", err
	}
	var best int64
	for _, e := range entries {
		if m := lkgName.FindStringSubmatch(e.Name()); m != nil {
			if v, _ := strconv.ParseInt(m[1], 10, 64); v > best {
				best = v
			}
		}
	}
	if best == 0 {
		return 0, "", nil
	}
	return best, p.path(fmt.Sprintf("lkg-%d.json", best)), nil
}

func (p *Pipeline) recover() error {
	for _, junk := range []string{"staging.json", "live.json.tmp"} {
		_ = os.Remove(p.path(junk))
	}
	ver, file, err := p.newestLKG()
	if err != nil {
		return err
	}
	if ver == 0 {
		if err := p.atomicWrite(p.path("live.json"), []byte(EmptyConfig)); err != nil {
			return err
		}
		p.ver, p.sha, p.live = 0, signing.Sum(EmptyConfig), &policy.Config{Routers: map[string]policy.Router{}}
		return nil
	}
	if err := p.restoreFrom(file); err != nil {
		return err
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return err
	}
	live, err := policy.Validate(string(data), p.cfg.Rules)
	if err != nil {
		// A last-known-good that no longer satisfies policy (rules tightened since) is not served.
		_ = p.atomicWrite(p.path("live.json"), []byte(EmptyConfig))
		p.ver, p.sha, p.live = ver, signing.Sum(EmptyConfig), &policy.Config{Routers: map[string]policy.Router{}}
		return nil
	}
	p.ver, p.sha, p.live = ver, signing.Sum(string(data)), live
	return nil
}

// Version is the live config version.
func (p *Pipeline) Version() int64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.ver
}

// WriteSynced writes data to path and fsyncs it.
func WriteSynced(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func syncDir(dir string) {
	if d, err := os.Open(dir); err == nil {
		_ = d.Sync()
		d.Close()
	}
}

func (p *Pipeline) atomicWrite(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := p.cfg.WriteFile(tmp, data); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	syncDir(filepath.Dir(path))
	return nil
}

// restoreFrom atomically makes live.json a hard link to src. Nothing is written, so this works on
// a full disk. Files are only ever replaced by rename, never modified in place, so sharing an
// inode is safe.
func (p *Pipeline) restoreFrom(src string) error {
	tmp := p.path("live.json.tmp")
	_ = os.Remove(tmp)
	if err := os.Link(src, tmp); err != nil {
		data, rerr := os.ReadFile(src)
		if rerr != nil {
			return rerr
		}
		if werr := WriteSynced(tmp, data); werr != nil { // filesystems without hard links
			return werr
		}
	}
	if err := os.Rename(tmp, p.path("live.json")); err != nil {
		return err
	}
	_ = os.Remove(tmp) // rename is a no-op, and keeps tmp, when live is already a link to src
	syncDir(p.cfg.Dir)
	return nil
}

func (p *Pipeline) crash(point string) bool { return p.cfg.CrashAt != nil && p.cfg.CrashAt(point) }

func (p *Pipeline) reject(kind Kind, err error) (int64, error) {
	p.cfg.Metrics.Rejected(string(kind))
	return p.ver, &Rejection{Kind: kind, Err: err}
}

// Apply runs the pipeline. It returns the live version after the call, which is the new one on
// success and the unchanged one on any error.
func (p *Pipeline) Apply(ctx context.Context, env signing.Envelope) (int64, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	t0 := time.Now()
	tr := p.cfg.Tracer
	attrs := map[string]any{"version": env.Version}

	end := tr.Start(ctx, "verify", attrs)
	if err := signing.Verify(p.cfg.Key, env); err != nil {
		end(err)
		return p.reject(KindSignature, err)
	}
	if env.Version == p.ver && env.SHA256 == p.sha {
		end(nil)
		return p.ver, nil // the control plane retried a push that had already landed
	}
	if env.Version <= p.ver {
		err := fmt.Errorf("version %d is not newer than the live version %d", env.Version, p.ver)
		end(err)
		return p.reject(KindReplay, err)
	}
	end(nil)

	end = tr.Start(ctx, "validate", attrs)
	next, err := policy.Validate(env.Body, p.cfg.Rules)
	end(err)
	if err != nil {
		return p.reject(KindInvalid, err)
	}

	end = tr.Start(ctx, "stage", attrs)
	staging := p.path("staging.json")
	if err := p.cfg.WriteFile(staging, []byte(env.Body)); err != nil {
		_ = os.Remove(staging)
		end(err)
		return p.reject(KindStorage, err)
	}
	end(nil)
	if p.crash("staged") {
		return p.ver, ErrCrashed
	}

	end = tr.Start(ctx, "swap", attrs)
	if err := os.Rename(staging, p.path("live.json")); err != nil {
		_ = os.Remove(staging)
		end(err)
		return p.reject(KindStorage, err)
	}
	syncDir(p.cfg.Dir)
	end(nil)
	if p.crash("swapped") {
		return p.ver, ErrCrashed
	}

	if err := p.verify(ctx, next, attrs); err != nil {
		return p.rollback(ctx, err, attrs)
	}
	if p.crash("verified") {
		// Killed between a verified swap and the commit's rename: restart restores the old config.
		return p.ver, ErrCrashed
	}

	end = tr.Start(ctx, "commit", attrs)
	if err := p.commit(env); err != nil {
		end(err)
		return p.rollback(ctx, fmt.Errorf("commit: %w", err), attrs)
	}
	end(nil)
	p.ver, p.sha, p.live = env.Version, env.SHA256, next
	if p.crash("committed") {
		return p.ver, ErrCrashed
	}
	p.cfg.Metrics.SetVersion(p.ver)
	p.cfg.Metrics.Applied()
	p.cfg.Metrics.ObserveReload(time.Since(t0).Seconds())
	return p.ver, nil
}

func (p *Pipeline) commit(env signing.Envelope) error {
	final := p.path(fmt.Sprintf("lkg-%d.json", env.Version))
	tmp := final + ".tmp"
	_ = os.Remove(tmp)
	if err := os.Link(p.path("live.json"), tmp); err != nil {
		if werr := p.cfg.WriteFile(tmp, []byte(env.Body)); werr != nil {
			return werr
		}
	}
	if err := os.Rename(tmp, final); err != nil { // the commit point
		_ = os.Remove(tmp)
		return err
	}
	syncDir(p.cfg.Dir)
	if old, _, _ := p.newestLKG(); old == env.Version {
		entries, _ := os.ReadDir(p.cfg.Dir)
		for _, e := range entries {
			if m := lkgName.FindStringSubmatch(e.Name()); m != nil && e.Name() != filepath.Base(final) {
				_ = os.Remove(p.path(e.Name()))
			}
		}
	}
	return nil
}

// verify waits for Traefik to serve exactly the new routers, then probes.
func (p *Pipeline) verify(ctx context.Context, next *policy.Config, attrs map[string]any) error {
	tr := p.cfg.Tracer
	end := tr.Start(ctx, "converge", attrs)
	err := p.waitRouters(ctx, next.Names())
	end(err)
	if err != nil {
		return err
	}

	probes := next.ChangedSince(p.live)
	if _, ok := next.Routers[CanaryRouter]; ok && !contains(probes, CanaryRouter) {
		probes = append(probes, CanaryRouter)
	}
	sort.Strings(probes)
	end = tr.Start(ctx, "probe", map[string]any{"version": attrs["version"], "routes": probes})
	for _, name := range probes {
		if err = p.probe(ctx, next.Routers[name].Host); err != nil {
			err = fmt.Errorf("route %s: %w", name, err)
			break
		}
	}
	end(err)
	return err
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func (p *Pipeline) waitRouters(ctx context.Context, want []string) error {
	ctx, cancel := context.WithTimeout(ctx, p.cfg.ConvergeTimeout)
	defer cancel()
	sort.Strings(want)
	var last []string
	for {
		got, err := p.cfg.Traefik.Routers(ctx)
		if err == nil {
			sort.Strings(got)
			if strings.Join(got, ",") == strings.Join(want, ",") {
				return nil
			}
			last = got
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("traefik did not converge on %v (serving %v, last error %v)", want, last, err)
		case <-time.After(p.cfg.PollInterval):
		}
	}
}

func (p *Pipeline) probe(ctx context.Context, host string) error {
	ctx, cancel := context.WithTimeout(ctx, p.cfg.ProbeBudget)
	defer cancel()
	var err error
	for {
		if err = p.cfg.Prober.Probe(ctx, host); err == nil {
			return nil
		}
		select {
		case <-ctx.Done():
			return err
		case <-time.After(p.cfg.PollInterval):
		}
	}
}

// rollback restores the last-known-good and waits for Traefik to serve it again.
func (p *Pipeline) rollback(ctx context.Context, cause error, attrs map[string]any) (int64, error) {
	end := p.cfg.Tracer.Start(ctx, "rollback", attrs)
	p.cfg.Metrics.Rollback()
	var err error
	if _, file, e := p.newestLKG(); e != nil {
		err = e
	} else if file == "" {
		err = p.atomicWrite(p.path("live.json"), []byte(EmptyConfig))
	} else {
		err = p.restoreFrom(file)
	}
	if err == nil {
		// Best effort: the gateway is already correct on disk; this only confirms Traefik caught
		// up, so it must not hold the caller for a whole converge timeout if Traefik is down.
		wait, cancel := context.WithTimeout(ctx, min(p.cfg.ConvergeTimeout, 2*time.Second))
		_ = p.waitRouters(wait, p.live.Names())
		cancel()
	}
	end(err)
	if err != nil {
		cause = fmt.Errorf("%w (and the rollback itself failed: %v)", cause, err)
	}
	p.cfg.Metrics.Rejected(string(KindRollback))
	return p.ver, &Rejection{Kind: KindRollback, Err: cause}
}

// -- HTTP clients for the real Traefik

// HTTPTraefik reads the file-provider routers from Traefik's API.
type HTTPTraefik struct {
	APIURL string
	Client *http.Client
}

func (h HTTPTraefik) Routers(ctx context.Context) ([]string, error) {
	c := h.Client
	if c == nil {
		c = &http.Client{Timeout: 2 * time.Second}
	}
	out := []string{}
	// Traefik pages this endpoint and names the next page in X-Next-Page.
	for page, pages := "1", 0; page != "" && pages < 1000; pages++ {
		url := strings.TrimRight(h.APIURL, "/") + "/api/http/routers?per_page=100&page=" + page
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		resp, err := c.Do(req)
		if err != nil {
			return nil, err
		}
		raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("traefik api answered %d", resp.StatusCode)
		}
		if err != nil {
			return nil, err
		}
		var routers []struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(raw, &routers); err != nil {
			return nil, err
		}
		for _, r := range routers {
			if name, ok := strings.CutSuffix(r.Name, "@file"); ok {
				out = append(out, name)
			}
		}
		next := resp.Header.Get("X-Next-Page")
		if next == page {
			break
		}
		page = next
	}
	return out, nil
}

// HTTPProber requests Base with the given Host header. 404 (no such route) and 502/503/504
// (dead backend) fail; anything else, including an application error, proves the route works.
type HTTPProber struct {
	Base   string
	Client *http.Client
}

func (h HTTPProber) Probe(ctx context.Context, host string) error {
	c := h.Client
	if c == nil {
		c = &http.Client{Timeout: time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.Base, nil)
	if err != nil {
		return err
	}
	req.Host = host
	resp, err := c.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
	switch resp.StatusCode {
	case http.StatusNotFound, http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		return fmt.Errorf("%s answered %d", host, resp.StatusCode)
	}
	return nil
}

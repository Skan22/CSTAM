package pipeline

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/canary"
	"github.com/felcloud/ipo/gateway-agent/internal/faketraefik"
	"github.com/felcloud/ipo/gateway-agent/internal/metrics"
	"github.com/felcloud/ipo/gateway-agent/internal/policy"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/trace"
)

var mids = map[string]bool{"secure-headers": true, "rate-limit": true, "compress": true}

type rig struct {
	t       *testing.T
	dir     string
	priv    ed25519.PrivateKey
	pub     ed25519.PublicKey
	traefik *faketraefik.Server
	spans   *trace.Tracer
	metrics *metrics.Registry
	p       *Pipeline
	backend *httptest.Server
	cfg     Config
}

func newRig(t *testing.T, mutate ...func(*Config)) *rig {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	dir := t.TempDir()
	tf := faketraefik.New(filepath.Join(dir, "live.json"))
	t.Cleanup(tf.Close)

	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "hello") }))
	t.Cleanup(backend.Close)
	cs := httptest.NewServer(canary.Handler())
	t.Cleanup(cs.Close)
	tf.Map(policy.CanaryBackend, strings.TrimPrefix(cs.URL, "http://"))
	for _, ip := range []string{"10.20.0.11", "10.20.0.12", "10.20.0.13"} {
		tf.Map(ip+":8080", strings.TrimPrefix(backend.URL, "http://"))
	}

	r := &rig{t: t, dir: dir, priv: priv, pub: pub, traefik: tf, backend: backend,
		spans: trace.New(nil, true), metrics: metrics.New()}
	r.cfg = Config{
		Dir: dir, Key: pub,
		Rules:   policy.Rules{CIDR: netip.MustParsePrefix("10.20.0.0/24"), Middlewares: mids},
		Traefik: HTTPTraefik{APIURL: tf.APIURL}, Prober: HTTPProber{Base: tf.WebURL},
		Tracer: r.spans, Metrics: r.metrics,
		ConvergeTimeout: time.Second, ProbeBudget: 300 * time.Millisecond, PollInterval: 2 * time.Millisecond,
	}
	for _, m := range mutate {
		m(&r.cfg)
	}
	r.p = r.open()
	return r
}

func (r *rig) open() *Pipeline {
	r.t.Helper()
	p, err := New(r.cfg)
	if err != nil {
		r.t.Fatal(err)
	}
	return p
}

func route(name, host, ip string) (string, string) {
	r := fmt.Sprintf("\"%s\":{\"entryPoints\":[\"web\"],\"rule\":\"Host(`%s`)\",\"service\":\"%s\"}", name, host, name)
	s := fmt.Sprintf("\"%s\":{\"loadBalancer\":{\"servers\":[{\"url\":\"http://%s:8080\"}]}}", name, ip)
	return r, s
}

type rt struct{ name, host, ip string }

func body(routes ...rt) string {
	rs, ss := []string{}, []string{}
	all := append([]rt{{"ipo-canary", "canary.felcloud.test", ""}}, routes...)
	for _, x := range all {
		r, s := route(x.name, x.host, x.ip)
		if x.ip == "" {
			s = fmt.Sprintf("\"%s\":{\"loadBalancer\":{\"servers\":[{\"url\":\"http://%s\"}]}}", x.name, policy.CanaryBackend)
		}
		rs, ss = append(rs, r), append(ss, s)
	}
	return `{"http":{"routers":{` + strings.Join(rs, ",") + `},"services":{` + strings.Join(ss, ",") + `}}}`
}

func (r *rig) env(version int64, b string) signing.Envelope { return signing.Sign(r.priv, version, b) }

func (r *rig) apply(version int64, b string) (int64, error) {
	return r.p.Apply(trace.With(nil2(), "trace-"+fmt.Sprint(version)), r.env(version, b))
}

func (r *rig) live() string {
	r.t.Helper()
	b, err := os.ReadFile(filepath.Join(r.dir, "live.json"))
	if err != nil {
		r.t.Fatal(err)
	}
	return string(b)
}

func kindOf(err error) Kind {
	var rej *Rejection
	if errors.As(err, &rej) {
		return rej.Kind
	}
	return ""
}

func (r *rig) get(host string) int {
	r.t.Helper()
	req, _ := http.NewRequest("GET", r.traefik.WebURL, nil)
	req.Host = host
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		r.t.Fatal(err)
	}
	resp.Body.Close()
	return resp.StatusCode
}

func TestAppliesAValidConfig(t *testing.T) {
	r := newRig(t)
	b := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	v, err := r.apply(1, b)
	if err != nil || v != 1 {
		t.Fatalf("v=%d err=%v", v, err)
	}
	if r.live() != b {
		t.Error("live file does not hold the config")
	}
	if got := r.get("a.felcloud.test"); got != 200 {
		t.Errorf("route answers %d", got)
	}
	if v, _, _ := r.metrics.Snapshot(); v != 1 {
		t.Errorf("metric version %d", v)
	}
	if r.p.Version() != 1 {
		t.Errorf("Version() = %d", r.p.Version())
	}
}

func TestEmitsASpanPerStageCarryingTheTraceID(t *testing.T) {
	r := newRig(t)
	if _, err := r.apply(3, body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})); err != nil {
		t.Fatal(err)
	}
	want := []string{"verify", "validate", "stage", "swap", "converge", "probe", "commit"}
	got := r.spans.Spans()
	if len(got) != len(want) {
		t.Fatalf("spans = %+v", got)
	}
	for i, s := range got {
		if s.Name != want[i] || s.TraceID != "trace-3" {
			t.Errorf("span %d = %s/%s", i, s.Name, s.TraceID)
		}
	}
}

func TestReapplyingTheLiveVersionIsAnAck(t *testing.T) {
	r := newRig(t)
	b := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, b); err != nil {
		t.Fatal(err)
	}
	before := len(r.spans.Spans())
	if v, err := r.apply(1, b); err != nil || v != 1 {
		t.Fatalf("re-push: v=%d err=%v", v, err)
	}
	if len(r.spans.Spans()) > before+1 { // only the verify span
		t.Error("a re-push must not run the swap again")
	}
}

func TestRejectsInvalidConfigsAndKeepsTheLiveOne(t *testing.T) {
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	cases := map[string]string{
		"invalid json":   `{"http":`,
		"yaml":           "http:\n  routers: {}\n",
		"duplicate host": body(rt{"t-a", "a.felcloud.test", "10.20.0.11"}, rt{"t-b", "a.felcloud.test", "10.20.0.12"}),
		"out of range":   body(rt{"t-b", "b.felcloud.test", "10.99.0.5"}),
	}
	for name, bad := range cases {
		t.Run(name, func(t *testing.T) {
			r := newRig(t)
			if _, err := r.apply(1, good); err != nil {
				t.Fatal(err)
			}
			v, err := r.apply(2, bad)
			if kindOf(err) != KindInvalid || v != 1 {
				t.Fatalf("v=%d err=%v", v, err)
			}
			if r.live() != good || r.p.Version() != 1 {
				t.Error("live config changed")
			}
			if _, err := os.Stat(filepath.Join(r.dir, "staging.json")); err == nil {
				t.Error("staging file left behind")
			}
		})
	}
}

func TestRejectsBadSignatures(t *testing.T) {
	r := newRig(t)
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, good); err != nil {
		t.Fatal(err)
	}
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	forged := signing.Sign(other, 2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	tampered := r.env(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	tampered.Body = body(rt{"t-b", "b.felcloud.test", "10.20.0.13"})
	for name, env := range map[string]signing.Envelope{"forged": forged, "tampered": tampered} {
		v, err := r.p.Apply(nil2(), env)
		if kindOf(err) != KindSignature || v != 1 {
			t.Errorf("%s: v=%d err=%v", name, v, err)
		}
	}
	if r.live() != good {
		t.Error("live config changed")
	}
}

func TestRejectsReplayedOlderVersions(t *testing.T) {
	r := newRig(t)
	old := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	newer := body(rt{"t-b", "b.felcloud.test", "10.20.0.12"})
	if _, err := r.apply(1, old); err != nil {
		t.Fatal(err)
	}
	if _, err := r.apply(2, newer); err != nil {
		t.Fatal(err)
	}
	// A validly signed old config, replayed by an attacker who captured it.
	v, err := r.apply(1, old)
	if kindOf(err) != KindReplay || v != 2 {
		t.Fatalf("v=%d err=%v", v, err)
	}
	// Same number, different content.
	v, err = r.p.Apply(nil2(), r.env(2, old))
	if kindOf(err) != KindReplay || v != 2 {
		t.Fatalf("same version: v=%d err=%v", v, err)
	}
	if r.live() != newer {
		t.Error("live config changed")
	}
}

func TestDeadBackendRollsBackWithinASecond(t *testing.T) {
	r := newRig(t)
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, good); err != nil {
		t.Fatal(err)
	}
	r.traefik.Unmap("10.20.0.12:8080") // the new team's VM is not answering
	start := time.Now()
	v, err := r.apply(2, body(rt{"t-a", "a.felcloud.test", "10.20.0.11"}, rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	elapsed := time.Since(start)
	if kindOf(err) != KindRollback || v != 1 {
		t.Fatalf("v=%d err=%v", v, err)
	}
	if r.live() != good || r.p.Version() != 1 {
		t.Error("live config was not restored")
	}
	if elapsed > time.Second {
		t.Errorf("failed swap took %v", elapsed)
	}
	if got := r.get("a.felcloud.test"); got != 200 {
		t.Errorf("the previous route no longer answers: %d", got)
	}
	if got := r.get("b.felcloud.test"); got != 404 {
		t.Errorf("the rejected route is still served: %d", got)
	}
	if _, rb, _ := r.metrics.Snapshot(); rb != 1 {
		t.Errorf("rollbacks metric = %d", rb)
	}
	// The rollback restores the config Traefik was serving without another proof of health.
	last := r.spans.Spans()[len(r.spans.Spans())-1]
	if last.Name != "rollback" {
		t.Errorf("last span %q", last.Name)
	}
}

func TestRollbackTimeIsMeasured(t *testing.T) {
	r := newRig(t)
	if _, err := r.apply(1, body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})); err != nil {
		t.Fatal(err)
	}
	r.traefik.Unmap("10.20.0.12:8080")
	_, _ = r.apply(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	for _, s := range r.spans.Spans() {
		if s.Name == "rollback" && s.DurationMS > 1000 {
			t.Errorf("rollback stage took %.0f ms", s.DurationMS)
		}
	}
}

func TestFailingCanaryRollsBack(t *testing.T) {
	r := newRig(t)
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, good); err != nil {
		t.Fatal(err)
	}
	r.traefik.Unmap(policy.CanaryBackend)
	_, err := r.apply(2, body(rt{"t-a", "a.felcloud.test", "10.20.0.11"}, rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	if kindOf(err) != KindRollback || r.live() != good {
		t.Fatalf("err=%v", err)
	}
}

func TestTraefikThatNeverReloadsRollsBack(t *testing.T) {
	r := newRig(t, func(c *Config) { c.ConvergeTimeout = 100 * time.Millisecond })
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, good); err != nil {
		t.Fatal(err)
	}
	r.traefik.Freeze()
	_, err := r.apply(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	if kindOf(err) != KindRollback || r.live() != good {
		t.Fatalf("err=%v", err)
	}
}

// -- crash recovery: the process dies at each point of the swap

func TestKilledAtAnyPointOfTheSwapRestoresLastKnownGood(t *testing.T) {
	for _, point := range []string{"staged", "swapped", "verified", "committed"} {
		t.Run(point, func(t *testing.T) {
			r := newRig(t)
			good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
			if _, err := r.apply(1, good); err != nil {
				t.Fatal(err)
			}
			r.cfg.CrashAt = func(p string) bool { return p == point }
			r.p = r.open()
			_, err := r.apply(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
			if !errors.Is(err, ErrCrashed) {
				t.Fatalf("err = %v", err)
			}

			// The process restarts.
			r.cfg.CrashAt = nil
			p, err := New(r.cfg)
			if err != nil {
				t.Fatal(err)
			}
			want, wantV := good, int64(1)
			if point == "committed" {
				// The commit's rename had landed before the process died.
				want, wantV = body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}), 2
			}
			if r.live() != want || p.Version() != wantV {
				t.Fatalf("live=%q version=%d", r.live(), p.Version())
			}
			for _, leftover := range []string{"staging.json", "live.json.tmp"} {
				if _, err := os.Stat(filepath.Join(r.dir, leftover)); err == nil {
					t.Errorf("%s left behind", leftover)
				}
			}
		})
	}
}

func TestAFreshAgentServesTheEmptyConfig(t *testing.T) {
	r := newRig(t)
	if r.live() != EmptyConfig || r.p.Version() != 0 {
		t.Fatalf("live=%q version=%d", r.live(), r.p.Version())
	}
}

func TestRestartResumesFromTheCommittedVersion(t *testing.T) {
	r := newRig(t)
	b := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(4, b); err != nil {
		t.Fatal(err)
	}
	p := r.open()
	if p.Version() != 4 || r.live() != b {
		t.Fatalf("version %d", p.Version())
	}
	if _, err := p.Apply(nil2(), r.env(3, b)); kindOf(err) != KindReplay {
		t.Errorf("replay after restart: %v", err)
	}
}

func TestOnlyTheNewestLastKnownGoodIsKept(t *testing.T) {
	r := newRig(t)
	for v := int64(1); v <= 3; v++ {
		if _, err := r.apply(v, body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})); err != nil {
			t.Fatal(err)
		}
		// same body, different versions: only the sha matters for re-push, version must grow
	}
	files, _ := filepath.Glob(filepath.Join(r.dir, "lkg-*.json"))
	if len(files) != 1 || !strings.HasSuffix(files[0], "lkg-3.json") {
		t.Fatalf("files = %v", files)
	}
}

// -- disk full

func TestDiskFullWhileStagingLeavesTheLiveConfigUntouched(t *testing.T) {
	var full sync.Mutex
	failing := false
	r := newRig(t, func(c *Config) {
		c.WriteFile = func(path string, data []byte) error {
			full.Lock()
			defer full.Unlock()
			if failing {
				// A partial write, as a full filesystem leaves behind.
				_ = os.WriteFile(path, data[:len(data)/2], 0o644)
				return &os.PathError{Op: "write", Path: path, Err: syscall.ENOSPC}
			}
			return WriteSynced(path, data)
		}
	})
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, good); err != nil {
		t.Fatal(err)
	}
	full.Lock()
	failing = true
	full.Unlock()

	v, err := r.apply(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	if kindOf(err) != KindStorage || !errors.Is(err, syscall.ENOSPC) || v != 1 {
		t.Fatalf("v=%d err=%v", v, err)
	}
	if r.live() != good {
		t.Error("live config changed")
	}
	if _, err := os.Stat(filepath.Join(r.dir, "staging.json")); err == nil {
		t.Error("partial staging file left behind")
	}
	if got := r.get("a.felcloud.test"); got != 200 {
		t.Errorf("gateway stopped serving: %d", got)
	}

	// Once space returns, the same version applies.
	full.Lock()
	failing = false
	full.Unlock()
	if v, err := r.apply(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"})); err != nil || v != 2 {
		t.Fatalf("retry: v=%d err=%v", v, err)
	}
}

func TestRollbackNeedsNoDiskSpace(t *testing.T) {
	full := false
	r := newRig(t, func(c *Config) {
		c.WriteFile = func(path string, data []byte) error {
			if full && strings.HasSuffix(path, "staging.json") == false {
				return syscall.ENOSPC
			}
			return WriteSynced(path, data)
		}
	})
	good := body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})
	if _, err := r.apply(1, good); err != nil {
		t.Fatal(err)
	}
	full = true // staging still fits (it is small), everything after does not
	r.traefik.Unmap("10.20.0.12:8080")
	_, err := r.apply(2, body(rt{"t-b", "b.felcloud.test", "10.20.0.12"}))
	if kindOf(err) != KindRollback || r.live() != good {
		t.Fatalf("err=%v live=%q", err, r.live())
	}
}

func TestConcurrentAppliesAreSerialised(t *testing.T) {
	r := newRig(t)
	var wg sync.WaitGroup
	for v := int64(1); v <= 8; v++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = r.apply(v, body(rt{"t-a", "a.felcloud.test", "10.20.0.11"}))
		}()
	}
	wg.Wait()
	if r.p.Version() < 1 || r.p.Version() > 8 {
		t.Fatalf("version %d", r.p.Version())
	}
	if _, err := r.apply(r.p.Version(), body(rt{"t-a", "a.felcloud.test", "10.20.0.11"})); err != nil {
		t.Errorf("state is inconsistent: %v", err)
	}
}

func nil2() context.Context { return context.Background() }

func TestHTTPTraefikFollowsPagination(t *testing.T) {
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page := r.URL.Query().Get("page")
		if page == "" || page == "1" {
			w.Header().Set("X-Next-Page", "2")
			fmt.Fprint(w, `[{"name":"a@file"},{"name":"api@internal"}]`)
			return
		}
		fmt.Fprint(w, `[{"name":"b@file"}]`)
	}))
	defer ts.Close()
	got, err := HTTPTraefik{APIURL: ts.URL}.Routers(context.Background())
	if err != nil || len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Fatalf("got %v, %v", got, err)
	}
}

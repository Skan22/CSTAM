package controlplane

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/trace"
)

type fakeAPI struct {
	mu         sync.Mutex
	logins     int32
	validToken string
	beats      []map[string]any
	latest     *signing.Envelope
}

func (f *fakeAPI) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/auth/login", func(w http.ResponseWriter, r *http.Request) {
		var in map[string]string
		_ = json.NewDecoder(r.Body).Decode(&in)
		if in["email"] != "gw@x" || in["password"] != "pw" {
			http.Error(w, "no", 401)
			return
		}
		n := atomic.AddInt32(&f.logins, 1)
		f.mu.Lock()
		tok := "tok" + string(rune('0'+n))
		f.validToken = tok
		f.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{"access_token": tok, "expires_in": 900})
	})
	authed := func(h http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			f.mu.Lock()
			ok := r.Header.Get("Authorization") == "Bearer "+f.validToken
			f.mu.Unlock()
			if !ok {
				http.Error(w, "expired", 401)
				return
			}
			h(w, r)
		}
	}
	mux.HandleFunc("POST /v1/gateways/{name}/heartbeat", authed(func(w http.ResponseWriter, r *http.Request) {
		var in map[string]any
		_ = json.NewDecoder(r.Body).Decode(&in)
		in["gateway"] = r.PathValue("name")
		f.mu.Lock()
		f.beats = append(f.beats, in)
		f.mu.Unlock()
		w.WriteHeader(200)
	}))
	mux.HandleFunc("GET /v1/config/latest", authed(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		if f.latest == nil {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(f.latest)
	}))
	return mux
}

func setup(t *testing.T) (*fakeAPI, *Client) {
	f := &fakeAPI{}
	ts := httptest.NewServer(f.handler())
	t.Cleanup(ts.Close)
	return f, &Client{BaseURL: ts.URL, Gateway: "gw-a", Email: "gw@x", Password: "pw"}
}

func TestHeartbeatLogsInAndReports(t *testing.T) {
	f, c := setup(t)
	if err := c.Heartbeat(context.Background(), "MASTER", 7); err != nil {
		t.Fatal(err)
	}
	if len(f.beats) != 1 || f.beats[0]["vrrp_state"] != "MASTER" || f.beats[0]["live_version"] != float64(7) || f.beats[0]["gateway"] != "gw-a" {
		t.Fatalf("beats %v", f.beats)
	}
}

func TestAnExpiredTokenTriggersOneRelogin(t *testing.T) {
	f, c := setup(t)
	_ = c.Heartbeat(context.Background(), "MASTER", 1)
	f.mu.Lock()
	f.validToken = "rotated"
	f.mu.Unlock()
	if err := c.Heartbeat(context.Background(), "MASTER", 2); err != nil {
		t.Fatal(err)
	}
	if atomic.LoadInt32(&f.logins) != 2 || len(f.beats) != 2 {
		t.Fatalf("logins=%d beats=%d", f.logins, len(f.beats))
	}
}

func TestWrongPasswordFails(t *testing.T) {
	_, c := setup(t)
	c.Password = "bad"
	if err := c.Heartbeat(context.Background(), "MASTER", 1); err == nil {
		t.Fatal("expected an error")
	}
}

func TestPullAppliesOnlyNewerVersions(t *testing.T) {
	f, c := setup(t)
	var live int64 = 2
	var applied []int64
	var traceID string
	l := &Loop{Client: c, Version: func() int64 { return live },
		Apply: func(ctx context.Context, env signing.Envelope) (int64, error) {
			applied = append(applied, env.Version)
			traceID = trace.ID(ctx)
			live = env.Version
			return live, nil
		}}

	if err := l.PullOnce(context.Background()); err != nil { // nothing compiled yet
		t.Fatal(err)
	}
	f.latest = &signing.Envelope{Version: 2, Body: "{}"}
	_ = l.PullOnce(context.Background())
	if len(applied) != 0 {
		t.Fatalf("applied the live version again: %v", applied)
	}
	f.latest = &signing.Envelope{Version: 3, Body: "{}"}
	if err := l.PullOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(applied) != 1 || applied[0] != 3 || traceID == "" {
		t.Fatalf("applied %v trace %q", applied, traceID)
	}
}

func TestRunHeartbeatsAndPullsUntilCancelled(t *testing.T) {
	f, c := setup(t)
	f.latest = &signing.Envelope{Version: 1, Body: "{}"}
	var live int64
	var mu sync.Mutex
	l := &Loop{Client: c, State: func() string { return "BACKUP" },
		Version: func() int64 { mu.Lock(); defer mu.Unlock(); return live },
		Apply: func(ctx context.Context, env signing.Envelope) (int64, error) {
			mu.Lock()
			defer mu.Unlock()
			live = env.Version
			return live, nil
		}}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { l.Run(ctx, 5*time.Millisecond, 5*time.Millisecond); close(done) }()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		v := live
		mu.Unlock()
		f.mu.Lock()
		n := len(f.beats)
		f.mu.Unlock()
		if v == 1 && n >= 2 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done
	mu.Lock()
	defer mu.Unlock()
	if live != 1 {
		t.Fatalf("never pulled: live=%d", live)
	}
}

func TestAVersionThatFailedIsNotPulledAgain(t *testing.T) {
	f, c := setup(t)
	var tries int
	l := &Loop{Client: c, Version: func() int64 { return 1 },
		Apply: func(ctx context.Context, env signing.Envelope) (int64, error) {
			tries++
			return 1, errors.New("rolled back")
		}}
	f.latest = &signing.Envelope{Version: 2, SHA256: "aa", Body: "{}"}
	for range 3 {
		_ = l.PullOnce(context.Background())
	}
	if tries != 1 {
		t.Fatalf("applied a failing version %d times", tries)
	}
	f.latest = &signing.Envelope{Version: 3, SHA256: "bb", Body: "{}"}
	_ = l.PullOnce(context.Background())
	if tries != 2 {
		t.Fatalf("a newer version must be tried: %d", tries)
	}
}

func TestHeartbeatsContinueWhileAnApplyIsRunning(t *testing.T) {
	f, c := setup(t)
	f.latest = &signing.Envelope{Version: 1, Body: "{}"}
	release := make(chan struct{})
	l := &Loop{Client: c, State: func() string { return "MASTER" }, Version: func() int64 { return 0 },
		Apply: func(ctx context.Context, env signing.Envelope) (int64, error) {
			<-release
			return 1, nil
		}}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { l.Run(ctx, 5*time.Millisecond, 5*time.Millisecond); close(done) }()
	defer func() { close(release); cancel(); <-done }()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		f.mu.Lock()
		n := len(f.beats)
		f.mu.Unlock()
		if n >= 3 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("no heartbeats while the apply was blocked")
}

// Command ipo-agent runs on each gateway VM next to Traefik and keepalived. It accepts signed
// config versions from the control plane, applies them safely, and reports its state.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/canary"
	"github.com/felcloud/ipo/gateway-agent/internal/controlplane"
	"github.com/felcloud/ipo/gateway-agent/internal/metrics"
	"github.com/felcloud/ipo/gateway-agent/internal/pipeline"
	"github.com/felcloud/ipo/gateway-agent/internal/policy"
	"github.com/felcloud/ipo/gateway-agent/internal/server"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/trace"
	"github.com/felcloud/ipo/gateway-agent/internal/vrrp"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Getenv); err != nil {
		log.Fatalf("ipo-agent: %v", err)
	}
}

func env(get func(string) string, key, def string) string {
	if v := get(key); v != "" {
		return v
	}
	return def
}

func seconds(get func(string) string, key string, def int) (time.Duration, error) {
	n, err := strconv.Atoi(env(get, key, strconv.Itoa(def)))
	if err != nil || n < 1 {
		return 0, fmt.Errorf("%s must be a positive number of seconds", key)
	}
	return time.Duration(n) * time.Second, nil
}

func run(ctx context.Context, get func(string) string) error {
	name := env(get, "IPO_AGENT_NAME", "")
	pubB64 := get("IPO_SIGNING_PUBLIC_KEY")
	if name == "" || pubB64 == "" {
		return errors.New("IPO_AGENT_NAME and IPO_SIGNING_PUBLIC_KEY are required")
	}
	pub, err := signing.ParsePublicKey(pubB64)
	if err != nil {
		return err
	}
	cidr, err := netip.ParsePrefix(env(get, "IPO_SANDBOX_CIDR", "10.20.0.0/24"))
	if err != nil {
		return fmt.Errorf("IPO_SANDBOX_CIDR: %w", err)
	}
	dir := env(get, "IPO_CONFIG_DIR", "/var/lib/ipo-agent")
	stateFile := env(get, "IPO_VRRP_STATE_FILE", "/run/ipo/vrrp_state")
	hbEvery, err := seconds(get, "IPO_HEARTBEAT_SECONDS", 5)
	if err != nil {
		return err
	}
	pullEvery, err := seconds(get, "IPO_PULL_SECONDS", 5)
	if err != nil {
		return err
	}

	reg := metrics.New()
	mon := vrrp.Monitor{StateFile: stateFile, FaultFile: env(get, "IPO_VRRP_FAULT_FILE", filepath.Join(filepath.Dir(stateFile), "fault"))}
	pipe, err := pipeline.New(pipeline.Config{
		Dir: dir, Key: pub,
		Rules:   policy.Rules{CIDR: cidr, Middlewares: map[string]bool{"secure-headers": true, "rate-limit": true, "compress": true}},
		Traefik: pipeline.HTTPTraefik{APIURL: env(get, "IPO_TRAEFIK_API", "http://127.0.0.1:8080")},
		Prober:  pipeline.HTTPProber{Base: env(get, "IPO_TRAEFIK_WEB", "http://127.0.0.1:80")},
		Tracer:  trace.New(os.Stderr, false), Metrics: reg,
	})
	if err != nil {
		return err
	}

	// The canary responder Traefik's ipo-canary route points at.
	csrv, _, err := canary.Serve(env(get, "IPO_CANARY_ADDR", canary.Addr))
	if err != nil {
		return fmt.Errorf("canary responder: %w", err)
	}
	defer csrv.Close()

	go mon.Watch(ctx, time.Second, reg.VRRP)

	insecure := get("IPO_AGENT_INSECURE") == "1"
	srv := &server.Server{Name: name, Pipeline: pipe, VRRP: mon, Metrics: reg, Insecure: insecure,
		AdminCNs: strings.Split(env(get, "IPO_AGENT_ADMIN_CNS", "ipo-control-plane"), ","), Started: time.Now()}
	httpSrv := &http.Server{Addr: env(get, "IPO_AGENT_LISTEN", ":8443"), Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}
	if !insecure {
		httpSrv.TLSConfig, err = server.ServerTLS(get("IPO_AGENT_CERT"), get("IPO_AGENT_KEY"), get("IPO_AGENT_CA"))
		if err != nil {
			return fmt.Errorf("mTLS: %w (set IPO_AGENT_INSECURE=1 only for local development)", err)
		}
	} else {
		log.Print("WARNING: IPO_AGENT_INSECURE=1, serving without TLS or client certificates")
	}

	// A plain listener, bound to the management address, for Prometheus and health checks.
	if addr := env(get, "IPO_METRICS_ADDR", "127.0.0.1:9100"); addr != "off" {
		mux := http.NewServeMux()
		mux.Handle("/", srv.Handler())
		ml, err := net.Listen("tcp", addr)
		if err != nil {
			return err
		}
		go func() { _ = (&http.Server{Handler: metricsOnly(mux), ReadHeaderTimeout: 5 * time.Second}).Serve(ml) }()
	}

	if cpURL := get("IPO_CP_URL"); cpURL != "" {
		loop := &controlplane.Loop{
			Client: &controlplane.Client{BaseURL: cpURL, Gateway: name, Email: get("IPO_CP_EMAIL"), Password: get("IPO_CP_PASSWORD")},
			Apply:  pipe.Apply, Version: pipe.Version, State: mon.State, Logf: log.Printf,
		}
		go loop.Run(ctx, hbEvery, pullEvery)
	}

	errc := make(chan error, 1)
	go func() {
		if insecure {
			errc <- httpSrv.ListenAndServe()
		} else {
			errc <- httpSrv.ListenAndServeTLS("", "")
		}
	}()
	log.Printf("ipo-agent %s listening on %s (live version %d)", name, httpSrv.Addr, pipe.Version())
	select {
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return httpSrv.Shutdown(shutdown)
	case err := <-errc:
		return err
	}
}

// metricsOnly limits the plain listener to read-only observability routes.
func metricsOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && (r.URL.Path == "/metrics" || r.URL.Path == "/healthz") {
			next.ServeHTTP(w, r)
			return
		}
		http.NotFound(w, r)
	})
}

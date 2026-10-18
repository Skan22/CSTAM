// Package server is the agent's mTLS HTTP interface.
//
//	PUT  /config   deliver a signed config version (the control plane)
//	GET  /status   live version, VRRP state, uptime
//	POST /fault    give up MASTER on purpose (admin certificates only)
//	GET  /metrics  Prometheus text
package server

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/metrics"
	"github.com/felcloud/ipo/gateway-agent/internal/pipeline"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/trace"
	"github.com/felcloud/ipo/gateway-agent/internal/vrrp"
)

const maxBody = 4 << 20

// Server serves the agent API.
type Server struct {
	Name     string
	Pipeline *pipeline.Pipeline
	VRRP     vrrp.Monitor
	Metrics  *metrics.Registry
	// AdminCNs lists the client-certificate common names allowed to call /fault.
	AdminCNs []string
	// Insecure serves without client certificates (development only): /fault is then open.
	Insecure bool
	Started  time.Time
	// Prober requests a host through this gateway's own Traefik, for POST /probe.
	Prober pipeline.Prober
}

// Handler returns the routes.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /config", s.putConfig)
	mux.HandleFunc("GET /status", s.status)
	mux.HandleFunc("POST /fault", s.fault)
	mux.HandleFunc("DELETE /fault", s.clearFault)
	mux.HandleFunc("POST /probe", s.probe)
	mux.HandleFunc("GET /metrics", s.metrics)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]string{"status": "ok"}) })
	return mux
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// statusFor maps a rejection to the HTTP status the control plane's Pusher understands: 4xx is a
// permanent rejection it will not retry, 5xx is retried.
func statusFor(err error) int {
	var rej *pipeline.Rejection
	if !errors.As(err, &rej) {
		return http.StatusInternalServerError
	}
	switch rej.Kind {
	case pipeline.KindSignature:
		return http.StatusForbidden
	case pipeline.KindReplay:
		return http.StatusConflict
	case pipeline.KindInvalid, pipeline.KindRollback:
		return http.StatusUnprocessableEntity
	case pipeline.KindStorage:
		if errors.Is(rej.Err, syscall.ENOSPC) {
			return http.StatusInsufficientStorage
		}
		return http.StatusInternalServerError
	}
	return http.StatusInternalServerError
}

func (s *Server) putConfig(w http.ResponseWriter, r *http.Request) {
	var env signing.Envelope
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxBody)).Decode(&env); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"live_version": s.Pipeline.Version(), "error": "malformed envelope: " + err.Error()})
		return
	}
	id := r.Header.Get("X-Request-ID")
	if id == "" {
		id = trace.NewID()
	}
	// The client may give up (the control plane's push timeout is short) but a swap that has
	// started must be finished or rolled back on its own terms, not cancelled halfway.
	live, err := s.Pipeline.Apply(trace.With(context.WithoutCancel(r.Context()), id), env)
	if err != nil {
		writeJSON(w, statusFor(err), map[string]any{"live_version": live, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"live_version": live})
}

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"gateway":        s.Name,
		"live_version":   s.Pipeline.Version(),
		"vrrp_state":     s.VRRP.State(),
		"faulted":        s.VRRP.Faulted(),
		"uptime_seconds": int(time.Since(s.Started).Seconds()),
	})
}

func (s *Server) isAdmin(r *http.Request) bool {
	if r.TLS == nil {
		return s.Insecure
	}
	if len(r.TLS.PeerCertificates) == 0 {
		return false
	}
	return slices.Contains(s.AdminCNs, r.TLS.PeerCertificates[0].Subject.CommonName)
}

func (s *Server) fault(w http.ResponseWriter, r *http.Request) {
	if !s.isAdmin(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "the fault endpoint needs an admin certificate"})
		return
	}
	if err := s.VRRP.Fault(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"faulted": true})
}

func (s *Server) clearFault(w http.ResponseWriter, r *http.Request) {
	if !s.isAdmin(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "the fault endpoint needs an admin certificate"})
		return
	}
	if err := s.VRRP.Clear(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"faulted": false})
}

// probe requests a team's host through this gateway's Traefik, once, for the control plane: the
// management network has no path to the VIP, so this is how registration learns that a new
// team's sandbox serves. ok means the backend answered; routed means Traefik knows the host.
func (s *Server) probe(w http.ResponseWriter, r *http.Request) {
	if !s.isAdmin(r) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "probing needs the control plane's certificate"})
		return
	}
	var req struct {
		Host string `json:"host"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 4096)).Decode(&req); err != nil || req.Host == "" ||
		len(req.Host) > 253 || strings.ContainsAny(req.Host, " /\r\n") {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "send {\"host\": \"<name>\"}"})
		return
	}
	if s.Prober == nil {
		writeJSON(w, http.StatusNotImplemented, map[string]string{"error": "no prober configured"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	err := s.Prober.Probe(ctx, req.Host)
	out := map[string]any{"ok": err == nil, "routed": err == nil || errors.Is(err, pipeline.ErrBackendDown)}
	if err != nil {
		out["error"] = err.Error()
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) metrics(w http.ResponseWriter, r *http.Request) {
	s.Metrics.VRRP(s.VRRP.State())
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	s.Metrics.Write(w)
}

// ServerTLS builds a config that requires and verifies a client certificate signed by caFile.
func ServerTLS(certFile, keyFile, caFile string) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, err
	}
	pool, err := loadPool(caFile)
	if err != nil {
		return nil, err
	}
	return &tls.Config{
		MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{cert},
		ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: pool,
	}, nil
}

// ClientTLS builds a config for the agent's calls to the control plane.
func ClientTLS(certFile, keyFile, caFile string) (*tls.Config, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS13}
	if caFile != "" {
		pool, err := loadPool(caFile)
		if err != nil {
			return nil, err
		}
		cfg.RootCAs = pool
	}
	if certFile != "" {
		cert, err := tls.LoadX509KeyPair(certFile, keyFile)
		if err != nil {
			return nil, err
		}
		cfg.Certificates = []tls.Certificate{cert}
	}
	return cfg, nil
}

func loadPool(caFile string) (*x509.CertPool, error) {
	pem, err := os.ReadFile(caFile)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("%s holds no PEM certificate", caFile)
	}
	return pool, nil
}

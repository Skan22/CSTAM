// Package canary is the responder behind the `ipo-canary` router. The agent probes it through
// Traefik after every swap: an answer proves the new config was loaded and routes end to end.
package canary

import (
	"net"
	"net/http"
	"time"
)

// Addr is where the responder listens; the compiler's canary route points here.
const Addr = "127.0.0.1:8082"

// Handler answers every request with 200.
func Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("ipo-canary ok\n"))
	})
}

// Serve listens on addr and returns the server so the caller can shut it down.
func Serve(addr string) (*http.Server, net.Addr, error) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, nil, err
	}
	srv := &http.Server{Handler: Handler(), ReadHeaderTimeout: 2 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	return srv, ln.Addr(), nil
}

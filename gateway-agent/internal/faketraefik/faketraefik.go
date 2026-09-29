// Package faketraefik is a stand-in for Traefik's file provider, API and web entry point.
//
// It watches the live config file the way Traefik does (picking up a rename within a few
// milliseconds and ignoring an invalid file), serves /api/http/routers, and proxies requests by
// Host header. Backends are mapped through Backends because the sandbox addresses (10.20.0.x) do
// not exist on a developer machine; an unmapped backend behaves like a dead VM.
package faketraefik

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

var hostRule = regexp.MustCompile("Host\\(`([^`]+)`\\)")

type route struct{ host, backend string }

// Server is a running fake.
type Server struct {
	APIURL string // /api/http/routers
	WebURL string // the "web" entry point

	Backends map[string]string // "10.20.0.11:8080" -> real "127.0.0.1:port"
	// Default, if set, serves every backend not in Backends (a developer machine has no sandbox
	// network). Unmap still kills a backend: it maps it to "" so the default does not apply.
	Default string

	file   string
	mu     sync.Mutex
	last   []byte
	routes map[string]route // by router name
	frozen bool
	api    *httptest.Server
	web    *httptest.Server
	stop   chan struct{}
	client *http.Client
}

// New starts a fake watching file.
func New(file string) *Server {
	s := &Server{file: file, Backends: map[string]string{}, routes: map[string]route{},
		stop: make(chan struct{}), client: &http.Client{Timeout: 500 * time.Millisecond}}
	s.api = httptest.NewServer(http.HandlerFunc(s.serveAPI))
	s.web = httptest.NewServer(http.HandlerFunc(s.serveWeb))
	s.APIURL, s.WebURL = s.api.URL, s.web.URL
	go s.watch()
	return s
}

// Close stops the fake.
func (s *Server) Close() {
	close(s.stop)
	s.api.Close()
	s.web.Close()
}

// Freeze makes the fake ignore config changes, like a Traefik that hung on reload.
func (s *Server) Freeze() { s.mu.Lock(); s.frozen = true; s.mu.Unlock() }

// Map routes a sandbox address to a real listener.
func (s *Server) Map(sandbox, real string) {
	s.mu.Lock()
	s.Backends[sandbox] = real
	s.mu.Unlock()
}

// Unmap makes a backend dead.
func (s *Server) Unmap(sandbox string) {
	s.mu.Lock()
	s.Backends[sandbox] = ""
	s.mu.Unlock()
}

func (s *Server) watch() {
	t := time.NewTicker(2 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-s.stop:
			return
		case <-t.C:
			s.reload()
		}
	}
}

type fileConfig struct {
	HTTP struct {
		Routers map[string]struct {
			Rule    string `json:"rule"`
			Service string `json:"service"`
		} `json:"routers"`
		Services map[string]struct {
			LoadBalancer struct {
				Servers []struct {
					URL string `json:"url"`
				} `json:"servers"`
			} `json:"loadBalancer"`
		} `json:"services"`
	} `json:"http"`
}

func (s *Server) reload() {
	data, err := os.ReadFile(s.file)
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil || s.frozen || string(data) == string(s.last) {
		return
	}
	var fc fileConfig
	if json.Unmarshal(data, &fc) != nil {
		return // Traefik keeps its previous configuration when the file does not parse
	}
	routes := map[string]route{}
	for name, r := range fc.HTTP.Routers {
		m := hostRule.FindStringSubmatch(r.Rule)
		if m == nil {
			continue
		}
		rt := route{host: strings.ToLower(m[1])}
		if svc, ok := fc.HTTP.Services[r.Service]; ok && len(svc.LoadBalancer.Servers) > 0 {
			if u, err := url.Parse(svc.LoadBalancer.Servers[0].URL); err == nil {
				rt.backend = u.Host
			}
		}
		routes[name] = rt
	}
	s.routes, s.last = routes, data
}

func (s *Server) serveAPI(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/api/http/routers" {
		http.NotFound(w, r)
		return
	}
	s.mu.Lock()
	names := make([]string, 0, len(s.routes))
	for n := range s.routes {
		names = append(names, n)
	}
	s.mu.Unlock()
	sort.Strings(names)
	out := []map[string]string{{"name": "api@internal", "status": "enabled"}}
	for _, n := range names {
		out = append(out, map[string]string{"name": n + "@file", "status": "enabled"})
	}
	_ = json.NewEncoder(w).Encode(out)
}

func (s *Server) serveWeb(w http.ResponseWriter, r *http.Request) {
	host := strings.ToLower(r.Host)
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	s.mu.Lock()
	var target string
	found := false
	for _, rt := range s.routes {
		if rt.host == host {
			found = true
			target = s.Backends[rt.backend]
			if _, mapped := s.Backends[rt.backend]; !mapped {
				target = s.Default
			}
		}
	}
	s.mu.Unlock()
	if !found {
		http.NotFound(w, r)
		return
	}
	if target == "" {
		http.Error(w, "bad gateway", http.StatusBadGateway)
		return
	}
	resp, err := s.client.Get("http://" + target + r.URL.Path)
	if err != nil {
		http.Error(w, "bad gateway", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

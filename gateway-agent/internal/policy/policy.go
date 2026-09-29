// Package policy validates a config body before the agent lets Traefik near it.
//
// The control plane already renders only what is safe, but the agent is the last line of
// defence: a compromised or buggy control plane must not be able to point a gateway at an
// arbitrary address, so the agent enforces the same rules independently.
package policy

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"net/url"
	"regexp"
	"sort"
	"strings"
)

var (
	ErrMalformed     = errors.New("config is not valid JSON of the expected shape")
	ErrDuplicateHost = errors.New("host is routed twice")
	ErrBackendRange  = errors.New("backend is not an address inside the sandbox range")
	ErrMiddleware    = errors.New("middleware is not allowed")
	ErrReference     = errors.New("router references an unknown service")
	ErrRule          = errors.New("rule must be a single Host(`name`)")
)

// CanaryBackend is the one loopback address a config may target: the agent's own responder.
const CanaryBackend = "127.0.0.1:8082"

var hostRule = regexp.MustCompile("^Host\\(`([^`]+)`\\)$")

// Rules are the constraints a config must satisfy.
type Rules struct {
	CIDR        netip.Prefix
	Middlewares map[string]bool
}

type rawRouter struct {
	EntryPoints []string `json:"entryPoints"`
	Middlewares []string `json:"middlewares"`
	Rule        string   `json:"rule"`
	Service     string   `json:"service"`
}

type rawService struct {
	LoadBalancer struct {
		Servers []struct {
			URL string `json:"url"`
		} `json:"servers"`
	} `json:"loadBalancer"`
}

type rawConfig struct {
	HTTP struct {
		Middlewares map[string]json.RawMessage `json:"middlewares"`
		Routers     map[string]rawRouter       `json:"routers"`
		Services    map[string]rawService      `json:"services"`
	} `json:"http"`
}

// Router is a validated route.
type Router struct {
	Host     string
	Backends []string // "ip:port", sorted
	Mids     []string
}

// Config is a validated config.
type Config struct {
	Routers map[string]Router
}

// Names returns the router names, sorted.
func (c *Config) Names() []string {
	if c == nil {
		return nil
	}
	out := make([]string, 0, len(c.Routers))
	for n := range c.Routers {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

// ChangedSince returns the routers that are new or differ from old, sorted. These are the routes
// the agent must probe after a swap; unchanged ones already proved themselves.
func (c *Config) ChangedSince(old *Config) []string {
	var out []string
	for _, n := range c.Names() {
		prev, ok := Router{}, false
		if old != nil {
			prev, ok = old.Routers[n]
		}
		if !ok || !sameRouter(prev, c.Routers[n]) {
			out = append(out, n)
		}
	}
	return out
}

func sameRouter(a, b Router) bool {
	return a.Host == b.Host && strings.Join(a.Backends, ",") == strings.Join(b.Backends, ",") &&
		strings.Join(a.Mids, ",") == strings.Join(b.Mids, ",")
}

func malformed(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrMalformed, fmt.Sprintf(format, args...))
}

// Validate parses body strictly and enforces rules.
func Validate(body string, rules Rules) (*Config, error) {
	dec := json.NewDecoder(strings.NewReader(body))
	dec.DisallowUnknownFields()
	var raw rawConfig
	if err := dec.Decode(&raw); err != nil {
		return nil, malformed("%v", err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, malformed("trailing data after the document")
	}

	declared := map[string]bool{}
	for name := range raw.HTTP.Middlewares {
		if !rules.Middlewares[name] {
			return nil, fmt.Errorf("%w: %q", ErrMiddleware, name)
		}
		if err := checkMiddlewareBody(name, raw.HTTP.Middlewares[name]); err != nil {
			return nil, err
		}
		declared[name] = true
	}

	cfg := &Config{Routers: map[string]Router{}}
	hosts := map[string]string{}
	for name, r := range raw.HTTP.Routers {
		m := hostRule.FindStringSubmatch(r.Rule)
		if m == nil {
			return nil, fmt.Errorf("%w: router %q has %q", ErrRule, name, r.Rule)
		}
		host := strings.ToLower(m[1])
		if other, dup := hosts[host]; dup {
			return nil, fmt.Errorf("%w: %q in %q and %q", ErrDuplicateHost, host, other, name)
		}
		hosts[host] = name
		for _, mw := range r.Middlewares {
			if !rules.Middlewares[mw] || !declared[mw] {
				return nil, fmt.Errorf("%w: router %q uses %q", ErrMiddleware, name, mw)
			}
		}
		svc, ok := raw.HTTP.Services[r.Service]
		if !ok {
			return nil, fmt.Errorf("%w: %q -> %q", ErrReference, name, r.Service)
		}
		var backends []string
		for _, s := range svc.LoadBalancer.Servers {
			b, err := backend(s.URL, rules.CIDR)
			if err != nil {
				return nil, fmt.Errorf("%w: service %q: %v", ErrBackendRange, r.Service, err)
			}
			backends = append(backends, b)
		}
		sort.Strings(backends)
		cfg.Routers[name] = Router{Host: host, Backends: backends, Mids: append([]string(nil), r.Middlewares...)}
	}
	return cfg, nil
}

func backend(raw string, cidr netip.Prefix) (string, error) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" || u.Port() == "" || u.Path != "" && u.Path != "/" {
		return "", fmt.Errorf("%q is not http://ip:port", raw)
	}
	addr, err := netip.ParseAddr(u.Hostname())
	if err != nil {
		return "", fmt.Errorf("%q is not an IP address", u.Hostname())
	}
	hostport := u.Host
	if hostport == CanaryBackend {
		return hostport, nil
	}
	if !cidr.Contains(addr) {
		return "", fmt.Errorf("%s is outside %s", addr, cidr)
	}
	return hostport, nil
}

// safeMiddlewareKinds are the Traefik middleware types a config may define. Anything that can
// send traffic or credentials elsewhere (forwardAuth, redirects, plugins, errors) is refused,
// so a signed config cannot use a middleware to get around the backend-range check.
var safeMiddlewareKinds = map[string]bool{"headers": true, "rateLimit": true, "compress": true}

func checkMiddlewareBody(name string, body json.RawMessage) error {
	var kinds map[string]json.RawMessage
	if err := json.Unmarshal(body, &kinds); err != nil {
		return fmt.Errorf("%w: %q is not an object", ErrMiddleware, name)
	}
	for kind := range kinds {
		if !safeMiddlewareKinds[kind] {
			return fmt.Errorf("%w: %q uses type %q", ErrMiddleware, name, kind)
		}
	}
	return nil
}

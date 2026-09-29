package policy

import (
	"errors"
	"net/netip"
	"strings"
	"testing"
)

var cidr = netip.MustParsePrefix("10.20.0.0/24")

const canary = "http://127.0.0.1:8082"

func cfg(routers, services string) string {
	return `{"http":{"middlewares":{"secure-headers":{}},"routers":{` + routers + `},"services":{` + services + `}}}`
}

func router(name, host, svc string, mws ...string) string {
	m := ""
	if len(mws) > 0 {
		m = `"middlewares":["` + strings.Join(mws, `","`) + `"],`
	}
	return `"` + name + `":{"entryPoints":["web"],` + m + `"rule":"Host(` + "`" + host + "`" + `)","service":"` + svc + `"}`
}

func service(name, url string) string {
	return `"` + name + `":{"loadBalancer":{"servers":[{"url":"` + url + `"}]}}`
}

func check(t *testing.T, body string, want error) {
	t.Helper()
	_, err := Validate(body, Rules{CIDR: cidr, Middlewares: map[string]bool{"secure-headers": true, "rate-limit": true, "compress": true}})
	if want == nil && err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if want != nil && !errors.Is(err, want) {
		t.Fatalf("want %v, got %v", want, err)
	}
}

func TestAcceptsARenderedConfig(t *testing.T) {
	body := `{"http":{"middlewares":{"secure-headers":{"headers":{}}},"routers":{` +
		router("t-a-x", "a.x", "t-a-x", "secure-headers") + "," + router("ipo-canary", "canary.x", "ipo-canary") +
		`},"services":{` + service("t-a-x", "http://10.20.0.11:8080") + "," + service("ipo-canary", canary) + `}}}`
	check(t, body, nil)
}

func TestAcceptsTheEmptyConfig(t *testing.T) { check(t, `{"http":{}}`, nil) }

func TestRejects(t *testing.T) {
	cases := []struct {
		name string
		body string
		want error
	}{
		{"not json", `{"http":`, ErrMalformed},
		{"yaml", "http:\n  routers: {}\n", ErrMalformed},
		{"trailing data", `{"http":{}} {}`, ErrMalformed},
		{"unknown top level key", `{"tcp":{}}`, ErrMalformed},
		{"duplicate host", cfg(router("a", "h.x", "s")+","+router("b", "h.x", "s"), service("s", "http://10.20.0.11:80")), ErrDuplicateHost},
		{"host differs in case", cfg(router("a", "H.x", "s")+","+router("b", "h.X", "s"), service("s", "http://10.20.0.11:80")), ErrDuplicateHost},
		{"backend outside range", cfg(router("a", "h.x", "s"), service("s", "http://10.99.0.5:80")), ErrBackendRange},
		{"backend is a hostname", cfg(router("a", "h.x", "s"), service("s", "http://evil.example:80")), ErrBackendRange},
		{"loopback other than canary", cfg(router("a", "h.x", "s"), service("s", "http://127.0.0.1:9999")), ErrBackendRange},
		{"metadata address", cfg(router("a", "h.x", "s"), service("s", "http://169.254.169.254:80")), ErrBackendRange},
		{"unknown middleware", cfg(router("a", "h.x", "s", "basic-auth"), service("s", "http://10.20.0.11:80")), ErrMiddleware},
		{"undeclared middleware use", cfg(router("a", "h.x", "s", "rate-limit"), service("s", "http://10.20.0.11:80")), ErrMiddleware},
		{"unknown service", cfg(router("a", "h.x", "nope"), service("s", "http://10.20.0.11:80")), ErrReference},
		{"rule is not a single Host", cfg(`"a":{"rule":"PathPrefix(`+"`/`"+`)","service":"s"}`, service("s", "http://10.20.0.11:80")), ErrRule},
		{"rule with boolean logic", cfg(`"a":{"rule":"Host(`+"`h.x`"+`) || Host(`+"`y.x`"+`)","service":"s"}`, service("s", "http://10.20.0.11:80")), ErrRule},
		{"non http backend", cfg(router("a", "h.x", "s"), service("s", "ftp://10.20.0.11:21")), ErrBackendRange},
		{"no backend port", cfg(router("a", "h.x", "s"), service("s", "http://10.20.0.11")), ErrBackendRange},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) { check(t, c.body, c.want) })
	}
}

func TestParsedConfigExposesRoutersAndBackends(t *testing.T) {
	body := cfg(router("t-a", "a.x", "t-a"), service("t-a", "http://10.20.0.11:8080"))
	c, err := Validate(body, Rules{CIDR: cidr, Middlewares: map[string]bool{"secure-headers": true}})
	if err != nil {
		t.Fatal(err)
	}
	r := c.Routers["t-a"]
	if r.Host != "a.x" || r.Backends[0] != "10.20.0.11:8080" {
		t.Fatalf("got %+v", r)
	}
	if len(c.Names()) != 1 || c.Names()[0] != "t-a" {
		t.Fatalf("names %v", c.Names())
	}
}

func TestChangedSince(t *testing.T) {
	rules := Rules{CIDR: cidr, Middlewares: map[string]bool{"secure-headers": true}}
	old, _ := Validate(cfg(router("a", "a.x", "a")+","+router("b", "b.x", "b"),
		service("a", "http://10.20.0.11:80")+","+service("b", "http://10.20.0.12:80")), rules)
	next, _ := Validate(cfg(router("a", "a.x", "a")+","+router("b", "b.x", "b")+","+router("c", "c.x", "c"),
		service("a", "http://10.20.0.11:80")+","+service("b", "http://10.20.0.99:80")+","+service("c", "http://10.20.0.13:80")), rules)
	got := next.ChangedSince(old)
	if len(got) != 2 || got[0] != "b" || got[1] != "c" {
		t.Fatalf("changed = %v", got)
	}
	if n := next.ChangedSince(nil); len(n) != 3 {
		t.Fatalf("against nil: %v", n)
	}
}

func TestMiddlewareBodiesAreLimitedToSafeKinds(t *testing.T) {
	route := router("a", "a.x", "a", "compress")
	svc := service("a", "http://10.20.0.11:80")
	for name, mw := range map[string]string{
		"forwardAuth": `{"forwardAuth":{"address":"http://evil.example"}}`,
		"redirect":    `{"redirectRegex":{"regex":".*","replacement":"http://evil.example"}}`,
		"plugin":      `{"plugin":{"x":{}}}`,
		"mixed":       `{"compress":{},"forwardAuth":{"address":"http://evil.example"}}`,
		"not object":  `"compress"`,
	} {
		body := `{"http":{"middlewares":{"compress":` + mw + `},"routers":{` + route + `},"services":{` + svc + `}}}`
		t.Run(name, func(t *testing.T) { check(t, body, ErrMiddleware) })
	}
	ok := `{"http":{"middlewares":{"compress":{"compress":{}},"rate-limit":{"rateLimit":{"average":1}}},"routers":{` + route + `},"services":{` + svc + `}}}`
	check(t, ok, nil)
}

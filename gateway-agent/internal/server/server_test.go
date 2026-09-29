package server

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/canary"
	"github.com/felcloud/ipo/gateway-agent/internal/faketraefik"
	"github.com/felcloud/ipo/gateway-agent/internal/metrics"
	"github.com/felcloud/ipo/gateway-agent/internal/pipeline"
	"github.com/felcloud/ipo/gateway-agent/internal/policy"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/vrrp"
)

// -- a throwaway PKI

type pki struct {
	dir string
	ca  *x509.Certificate
	key *ecdsa.PrivateKey
}

func newPKI(t *testing.T) *pki {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test-ca"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour), IsCA: true,
		BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	der, _ := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	ca, _ := x509.ParseCertificate(der)
	p := &pki{dir: t.TempDir(), ca: ca, key: key}
	writePEM(t, filepath.Join(p.dir, "ca.pem"), "CERTIFICATE", der)
	return p
}

func writePEM(t *testing.T, path, typ string, der []byte) {
	t.Helper()
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
}

// issue writes <name>.pem and <name>.key and returns their paths.
func (p *pki) issue(t *testing.T, name string, serial int64, usage x509.ExtKeyUsage) (string, string) {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: name},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		ExtKeyUsage: []x509.ExtKeyUsage{usage}, KeyUsage: x509.KeyUsageDigitalSignature,
		DNSNames: []string{"localhost"}, IPAddresses: nil}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, p.ca, &key.PublicKey, p.key)
	if err != nil {
		t.Fatal(err)
	}
	kd, _ := x509.MarshalECPrivateKey(key)
	c, k := filepath.Join(p.dir, name+".pem"), filepath.Join(p.dir, name+".key")
	writePEM(t, c, "CERTIFICATE", der)
	writePEM(t, k, "EC PRIVATE KEY", kd)
	return c, k
}

// -- the agent under test

type agent struct {
	srv   *Server
	priv  ed25519.PrivateKey
	dir   string
	state string
}

func newAgent(t *testing.T) *agent {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	dir := t.TempDir()
	tf := faketraefik.New(filepath.Join(dir, "live.yml"))
	t.Cleanup(tf.Close)
	cs := httptest.NewServer(canary.Handler())
	t.Cleanup(cs.Close)
	tf.Map(policy.CanaryBackend, strings.TrimPrefix(cs.URL, "http://"))
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "ok") }))
	t.Cleanup(app.Close)
	tf.Map("10.20.0.11:8080", strings.TrimPrefix(app.URL, "http://"))
	m := metrics.New()
	p, err := pipeline.New(pipeline.Config{
		Dir: dir, Key: pub,
		Rules:   policy.Rules{CIDR: netip.MustParsePrefix("10.20.0.0/24"), Middlewares: map[string]bool{"compress": true}},
		Traefik: pipeline.HTTPTraefik{APIURL: tf.APIURL}, Prober: pipeline.HTTPProber{Base: tf.WebURL},
		Metrics: m, ConvergeTimeout: time.Second, ProbeBudget: 200 * time.Millisecond, PollInterval: 2 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	vdir := t.TempDir()
	return &agent{priv: priv, dir: dir, state: filepath.Join(vdir, "state"), srv: &Server{
		Name: "gw-a", Pipeline: p, Metrics: m, Started: time.Now(), AdminCNs: []string{"ipo-admin"},
		VRRP: vrrp.Monitor{StateFile: filepath.Join(vdir, "state"), FaultFile: filepath.Join(vdir, "fault")},
	}}
}

func body(routes ...string) string {
	rs := []string{`"ipo-canary":{"entryPoints":["web"],"rule":"Host(` + "`canary.x`" + `)","service":"ipo-canary"}`}
	ss := []string{`"ipo-canary":{"loadBalancer":{"servers":[{"url":"http://127.0.0.1:8082"}]}}`}
	for _, host := range routes {
		rs = append(rs, fmt.Sprintf(`"t-%s":{"entryPoints":["web"],"rule":"Host(`+"`%s`"+`)","service":"t-%s"}`, host, host, host))
		ss = append(ss, fmt.Sprintf(`"t-%s":{"loadBalancer":{"servers":[{"url":"http://10.20.0.11:8080"}]}}`, host))
	}
	return `{"http":{"routers":{` + strings.Join(rs, ",") + `},"services":{` + strings.Join(ss, ",") + `}}}`
}

func put(t *testing.T, c *http.Client, url string, env any) (int, map[string]any) {
	t.Helper()
	raw, _ := json.Marshal(env)
	req, _ := http.NewRequest(http.MethodPut, url+"/config", bytes.NewReader(raw))
	req.Header.Set("X-Request-ID", "req-1")
	resp, err := c.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	b, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(b, &out)
	return resp.StatusCode, out
}

func TestPutConfigStatusCodes(t *testing.T) {
	a := newAgent(t)
	ts := httptest.NewServer(a.srv.Handler())
	defer ts.Close()

	code, out := put(t, ts.Client(), ts.URL, signing.Sign(a.priv, 1, body("a.x")))
	if code != 200 || out["live_version"] != float64(1) {
		t.Fatalf("apply: %d %v", code, out)
	}
	// A retried push of the same version is an ack.
	if code, _ = put(t, ts.Client(), ts.URL, signing.Sign(a.priv, 1, body("a.x"))); code != 200 {
		t.Errorf("retry: %d", code)
	}

	_, other, _ := ed25519.GenerateKey(rand.Reader)
	stale := signing.Sign(a.priv, 1, body("b.x"))
	for name, tc := range map[string]struct {
		env  any
		want int
	}{
		"bad signature": {signing.Sign(other, 2, body("b.x")), 403},
		"replay":        {stale, 409},
		"policy":        {signing.Sign(a.priv, 2, `{"http":{"routers":{"r":{"rule":"Host(`+"`h`"+`)","service":"nope"}}}}`), 422},
		"malformed":     {map[string]any{"version": "x"}, 400},
	} {
		code, out := put(t, ts.Client(), ts.URL, tc.env)
		if code != tc.want || out["live_version"] == nil && code != 400 {
			t.Errorf("%s: %d %v", name, code, out)
		}
		if code != 400 && out["live_version"] != float64(1) {
			t.Errorf("%s: live_version %v", name, out["live_version"])
		}
		if out["error"] == nil {
			t.Errorf("%s: no error text", name)
		}
	}
}

func TestOversizedBodyIsRefused(t *testing.T) {
	a := newAgent(t)
	ts := httptest.NewServer(a.srv.Handler())
	defer ts.Close()
	big := signing.Envelope{Version: 1, Body: strings.Repeat("x", maxBody+1)}
	if code, _ := put(t, ts.Client(), ts.URL, big); code != 400 {
		t.Errorf("code %d", code)
	}
}

func TestStatusAndMetrics(t *testing.T) {
	a := newAgent(t)
	_ = os.WriteFile(a.state, []byte("MASTER"), 0o644)
	ts := httptest.NewServer(a.srv.Handler())
	defer ts.Close()
	put(t, ts.Client(), ts.URL, signing.Sign(a.priv, 5, body("a.x")))

	resp, _ := http.Get(ts.URL + "/status")
	var st map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&st)
	if st["live_version"] != float64(5) || st["vrrp_state"] != "MASTER" || st["gateway"] != "gw-a" {
		t.Fatalf("status %v", st)
	}

	resp, _ = http.Get(ts.URL + "/metrics")
	text, _ := io.ReadAll(resp.Body)
	for _, want := range []string{"ipo_agent_config_version 5", "ipo_agent_rollbacks_total 0",
		"ipo_agent_vrrp_transitions_total 0", "ipo_agent_reload_duration_seconds_count 1"} {
		if !strings.Contains(string(text), want) {
			t.Errorf("metrics lack %q:\n%s", want, text)
		}
	}
	_ = os.WriteFile(a.state, []byte("BACKUP"), 0o644)
	resp, _ = http.Get(ts.URL + "/metrics")
	text, _ = io.ReadAll(resp.Body)
	if !strings.Contains(string(text), "ipo_agent_vrrp_transitions_total 1") {
		t.Errorf("transition not counted:\n%s", text)
	}
}

func TestFaultNeedsAnAdminCertificate(t *testing.T) {
	a := newAgent(t)
	ca := newPKI(t)
	scert, skey := ca.issue(t, "agent", 10, x509.ExtKeyUsageServerAuth)
	tlsCfg, err := ServerTLS(scert, skey, filepath.Join(ca.dir, "ca.pem"))
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewUnstartedServer(a.srv.Handler())
	ts.TLS = tlsCfg
	ts.StartTLS()
	defer ts.Close()

	client := func(cn string, serial int64, pk *pki) *http.Client {
		c, k := pk.issue(t, cn, serial, x509.ExtKeyUsageClientAuth)
		cfg, err := ClientTLS(c, k, filepath.Join(ca.dir, "ca.pem"))
		if err != nil {
			t.Fatal(err)
		}
		cfg.ServerName = "localhost"
		return &http.Client{Transport: &http.Transport{TLSClientConfig: cfg}, Timeout: 2 * time.Second}
	}

	admin := client("ipo-admin", 11, ca)
	plain := client("ipo-control-plane", 12, ca)

	resp, err := plain.Post(ts.URL+"/fault", "", nil)
	if err != nil || resp.StatusCode != 403 {
		t.Fatalf("non-admin: %v %v", resp, err)
	}
	if a.srv.VRRP.Faulted() {
		t.Fatal("a non-admin injected a fault")
	}
	resp, err = admin.Post(ts.URL+"/fault", "", nil)
	if err != nil || resp.StatusCode != 200 || !a.srv.VRRP.Faulted() {
		t.Fatalf("admin: %v %v", resp, err)
	}
	req, _ := http.NewRequest(http.MethodDelete, ts.URL+"/fault", nil)
	if resp, err = admin.Do(req); err != nil || resp.StatusCode != 200 || a.srv.VRRP.Faulted() {
		t.Fatalf("clear: %v %v", resp, err)
	}

	// No client certificate: the handshake itself fails.
	bare, _ := ClientTLS("", "", filepath.Join(ca.dir, "ca.pem"))
	bare.ServerName = "localhost"
	_, err = (&http.Client{Transport: &http.Transport{TLSClientConfig: bare}}).Get(ts.URL + "/status")
	if err == nil {
		t.Error("a client without a certificate was served")
	}

	// A certificate from another CA.
	rogue := newPKI(t)
	c, k := rogue.issue(t, "ipo-admin", 13, x509.ExtKeyUsageClientAuth)
	cfg, _ := ClientTLS(c, k, filepath.Join(ca.dir, "ca.pem"))
	cfg.ServerName = "localhost"
	_, err = (&http.Client{Transport: &http.Transport{TLSClientConfig: cfg}}).Get(ts.URL + "/status")
	if err == nil {
		t.Error("a certificate from an unknown CA was served")
	}
	_ = tls.VersionTLS13
}

func TestInsecureModeOpensFaultOnly(t *testing.T) {
	a := newAgent(t)
	ts := httptest.NewServer(a.srv.Handler())
	defer ts.Close()
	if resp, _ := http.Post(ts.URL+"/fault", "", nil); resp.StatusCode != 403 {
		t.Errorf("without Insecure: %d", resp.StatusCode)
	}
	a.srv.Insecure = true
	if resp, _ := http.Post(ts.URL+"/fault", "", nil); resp.StatusCode != 200 {
		t.Errorf("with Insecure: %d", resp.StatusCode)
	}
}

// A client that gives up (the control plane's push timeout) must not turn a good config into a
// rollback: once the swap has happened the apply finishes without the request's context.
func TestApplyOutlivesTheClientConnection(t *testing.T) {
	a := newAgent(t)
	ctx, cancel := context.WithCancel(context.Background())
	raw, _ := json.Marshal(signing.Sign(a.priv, 1, body("a.x")))
	req := httptest.NewRequest(http.MethodPut, "/config", bytes.NewReader(raw)).WithContext(ctx)
	cancel()
	rec := httptest.NewRecorder()
	a.srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 200 || a.srv.Pipeline.Version() != 1 {
		t.Fatalf("status %d live %d: %s", rec.Code, a.srv.Pipeline.Version(), rec.Body)
	}
}

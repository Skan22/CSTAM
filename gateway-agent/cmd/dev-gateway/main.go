// Command dev-gateway is a whole gateway for a developer machine: the real agent pipeline in
// front of a fake Traefik whose backends all answer 200. It lets the control plane's HttpAgent
// be exercised without OpenStack, Traefik or keepalived.
//
// It prints "LISTENING <addr>" once ready. Plain HTTP; never run this in production.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/canary"
	"github.com/felcloud/ipo/gateway-agent/internal/faketraefik"
	"github.com/felcloud/ipo/gateway-agent/internal/metrics"
	"github.com/felcloud/ipo/gateway-agent/internal/pipeline"
	"github.com/felcloud/ipo/gateway-agent/internal/policy"
	"github.com/felcloud/ipo/gateway-agent/internal/server"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/vrrp"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:0", "address to listen on")
	pubB64 := flag.String("pubkey", "", "base64 Ed25519 public key of the control plane")
	cidr := flag.String("cidr", "10.20.0.0/24", "sandbox network")
	dir := flag.String("dir", "", "state directory (default: a temporary one)")
	name := flag.String("name", "dev-gateway", "gateway name")
	flag.Parse()

	pub, err := signing.ParsePublicKey(*pubB64)
	if err != nil {
		log.Fatal(err)
	}
	if *dir == "" {
		if *dir, err = os.MkdirTemp("", "dev-gateway-"); err != nil {
			log.Fatal(err)
		}
		defer os.RemoveAll(*dir)
	}
	tf := faketraefik.New(filepath.Join(*dir, "live.yml"))
	defer tf.Close()
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "sandbox") }))
	defer app.Close()
	cs := httptest.NewServer(canary.Handler())
	defer cs.Close()
	tf.Default = strings.TrimPrefix(app.URL, "http://")
	tf.Map(policy.CanaryBackend, strings.TrimPrefix(cs.URL, "http://"))

	reg := metrics.New()
	pipe, err := pipeline.New(pipeline.Config{
		Dir: *dir, Key: pub,
		Rules:   policy.Rules{CIDR: netip.MustParsePrefix(*cidr), Middlewares: map[string]bool{"secure-headers": true, "rate-limit": true, "compress": true}},
		Traefik: pipeline.HTTPTraefik{APIURL: tf.APIURL}, Prober: pipeline.HTTPProber{Base: tf.WebURL},
		Metrics: reg, ConvergeTimeout: 2 * time.Second, ProbeBudget: time.Second, PollInterval: 5 * time.Millisecond,
	})
	if err != nil {
		log.Fatal(err)
	}
	mon := vrrp.Monitor{StateFile: filepath.Join(*dir, "vrrp_state"), FaultFile: filepath.Join(*dir, "fault")}
	_ = os.WriteFile(mon.StateFile, []byte("MASTER"), 0o644)
	srv := &server.Server{Name: *name, Pipeline: pipe, VRRP: mon, Metrics: reg, Insecure: true, Started: time.Now()}

	ln, err := netListen(*listen)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("LISTENING", ln.Addr())
	os.Stdout.Sync()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	hs := &http.Server{Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}
	go func() { <-ctx.Done(); _ = hs.Close() }()
	_ = hs.Serve(ln)
}

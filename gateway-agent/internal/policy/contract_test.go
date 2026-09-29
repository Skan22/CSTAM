package policy_test

import (
	"encoding/json"
	"net/netip"
	"os"
	"slices"
	"testing"

	"github.com/felcloud/ipo/gateway-agent/internal/policy"
	"github.com/felcloud/ipo/gateway-agent/internal/signing"
)

// The fixture is produced by the Python compiler (control-plane/tests/test_agent_contract.py).
// If either side changes the wire format, this test or that one fails.
func TestAcceptsWhatThePythonCompilerProduces(t *testing.T) {
	raw, err := os.ReadFile("../../testdata/python_envelope.json")
	if err != nil {
		t.Fatal(err)
	}
	var fx struct {
		PublicKey   string           `json:"public_key"`
		CIDR        string           `json:"cidr"`
		Middlewares []string         `json:"middlewares"`
		Routers     []string         `json:"routers"`
		Envelope    signing.Envelope `json:"envelope"`
	}
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	pub, err := signing.ParsePublicKey(fx.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := signing.Verify(pub, fx.Envelope); err != nil {
		t.Fatalf("Go rejects a Python signature: %v", err)
	}
	mids := map[string]bool{}
	for _, m := range fx.Middlewares {
		mids[m] = true
	}
	cfg, err := policy.Validate(fx.Envelope.Body, policy.Rules{CIDR: netip.MustParsePrefix(fx.CIDR), Middlewares: mids})
	if err != nil {
		t.Fatalf("Go policy rejects the compiler's output: %v", err)
	}
	if got := cfg.Names(); !slices.Equal(got, fx.Routers) {
		t.Fatalf("routers %v, want %v", got, fx.Routers)
	}
}

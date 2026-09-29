package signing

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"testing"
)

func keys(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv
}

func TestValidEnvelopeVerifies(t *testing.T) {
	pub, priv := keys(t)
	if err := Verify(pub, Sign(priv, 7, `{"http":{}}`)); err != nil {
		t.Fatal(err)
	}
}

func TestTamperingIsRejected(t *testing.T) {
	pub, priv := keys(t)
	good := Sign(priv, 7, `{"http":{}}`)

	body := good
	body.Body = `{"http":{"evil":1}}`
	if err := Verify(pub, body); !errors.Is(err, ErrBadHash) {
		t.Errorf("tampered body: %v", err)
	}

	// The attacker fixes up the hash but cannot forge the signature.
	rehash := body
	rehash.SHA256 = Sum(rehash.Body)
	if err := Verify(pub, rehash); !errors.Is(err, ErrBadSignature) {
		t.Errorf("rehashed body: %v", err)
	}

	relabel := good
	relabel.Version = 99 // a replayed old body under a newer number
	if err := Verify(pub, relabel); !errors.Is(err, ErrBadSignature) {
		t.Errorf("relabelled version: %v", err)
	}

	other, _ := keys(t)
	if err := Verify(other, good); !errors.Is(err, ErrBadSignature) {
		t.Errorf("wrong key: %v", err)
	}

	junk := good
	junk.Signature = "not base64 !!"
	if err := Verify(pub, junk); !errors.Is(err, ErrBadSignature) {
		t.Errorf("junk signature: %v", err)
	}
}

func TestParsePublicKey(t *testing.T) {
	pub, _ := keys(t)
	got, err := ParsePublicKey(base64.StdEncoding.EncodeToString(pub))
	if err != nil || !pub.Equal(got) {
		t.Fatalf("round trip failed: %v", err)
	}
	for _, bad := range []string{"", "AAAA", "!!!"} {
		if _, err := ParsePublicKey(bad); !errors.Is(err, ErrBadKey) {
			t.Errorf("%q: %v", bad, err)
		}
	}
}

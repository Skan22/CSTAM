// Package signing verifies the control plane's Ed25519 signature over a config version.
//
// The signed message is "ipo-config\n<version>\n<sha256>\n<body>", which binds the body to its
// version number: a tampered body, a body relabelled with another version and an old version
// replayed under a newer number all fail verification.
package signing

import (
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
)

var (
	ErrBadKey       = errors.New("signing: public key must be 32 bytes of base64")
	ErrBadHash      = errors.New("signing: sha256 does not match the body")
	ErrBadSignature = errors.New("signing: signature does not verify")
)

// Envelope is what the control plane pushes.
type Envelope struct {
	Version   int64  `json:"version"`
	SHA256    string `json:"sha256"`
	Signature string `json:"signature"`
	Body      string `json:"body"`
}

func message(version int64, sum, body string) []byte {
	return []byte(fmt.Sprintf("ipo-config\n%d\n%s\n%s", version, sum, body))
}

// Sum returns the hex SHA-256 of body.
func Sum(body string) string {
	h := sha256.Sum256([]byte(body))
	return hex.EncodeToString(h[:])
}

// ParsePublicKey decodes the base64 raw Ed25519 public key the control plane publishes.
func ParsePublicKey(b64 string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.DecodeString(b64)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, ErrBadKey
	}
	return ed25519.PublicKey(raw), nil
}

// Verify checks the envelope's hash and signature.
func Verify(pub ed25519.PublicKey, e Envelope) error {
	if subtle.ConstantTimeCompare([]byte(Sum(e.Body)), []byte(e.SHA256)) != 1 {
		return ErrBadHash
	}
	sig, err := base64.StdEncoding.DecodeString(e.Signature)
	if err != nil || !ed25519.Verify(pub, message(e.Version, e.SHA256, e.Body), sig) {
		return ErrBadSignature
	}
	return nil
}

// Sign is used by tests and tooling; the control plane holds the only production key.
func Sign(priv ed25519.PrivateKey, version int64, body string) Envelope {
	sum := Sum(body)
	sig := ed25519.Sign(priv, message(version, sum, body))
	return Envelope{Version: version, SHA256: sum, Signature: base64.StdEncoding.EncodeToString(sig), Body: body}
}

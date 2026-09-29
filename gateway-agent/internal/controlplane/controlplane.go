// Package controlplane is the agent's client for the control plane API: it reports a heartbeat
// and pulls the latest config in case a push was missed.
package controlplane

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/felcloud/ipo/gateway-agent/internal/signing"
	"github.com/felcloud/ipo/gateway-agent/internal/trace"
	"github.com/felcloud/ipo/gateway-agent/internal/traffic"
)

// ErrNoConfig means the control plane has not compiled any config yet.
var ErrNoConfig = errors.New("the control plane has no config yet")

// Client talks to the API, logging in with a service account and re-logging in when the 15
// minute token is refused.
type Client struct {
	BaseURL  string
	Gateway  string
	Email    string
	Password string
	HTTP     *http.Client

	mu    sync.Mutex
	token string
}

func (c *Client) http() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return &http.Client{Timeout: 5 * time.Second}
}

func (c *Client) login(ctx context.Context) error {
	raw, _ := json.Marshal(map[string]string{"email": c.Email, "password": c.Password})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/v1/auth/login", bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http().Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("login answered %d", resp.StatusCode)
	}
	var tok struct {
		AccessToken string `json:"access_token"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<16)).Decode(&tok); err != nil || tok.AccessToken == "" {
		return errors.New("login returned no token")
	}
	c.mu.Lock()
	c.token = tok.AccessToken
	c.mu.Unlock()
	return nil
}

// do sends an authenticated request, logging in first and once more if the token is refused.
func (c *Client) do(ctx context.Context, method, path string, body any) (*http.Response, error) {
	var raw []byte
	if body != nil {
		raw, _ = json.Marshal(body)
	}
	for attempt := 0; ; attempt++ {
		c.mu.Lock()
		tok := c.token
		c.mu.Unlock()
		if tok == "" {
			if err := c.login(ctx); err != nil {
				return nil, err
			}
			c.mu.Lock()
			tok = c.token
			c.mu.Unlock()
		}
		req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, bytes.NewReader(raw))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Authorization", "Bearer "+tok)
		if body != nil {
			req.Header.Set("Content-Type", "application/json")
		}
		resp, err := c.http().Do(req)
		if err != nil {
			return nil, err
		}
		if resp.StatusCode == http.StatusUnauthorized && attempt == 0 {
			resp.Body.Close()
			c.mu.Lock()
			c.token = ""
			c.mu.Unlock()
			continue
		}
		return resp, nil
	}
}

// Heartbeat reports the gateway's VRRP state and live config version.
func (c *Client) Heartbeat(ctx context.Context, vrrpState string, version int64) error {
	resp, err := c.do(ctx, http.MethodPost, "/v1/gateways/"+url.PathEscape(c.Gateway)+"/heartbeat",
		map[string]any{"vrrp_state": vrrpState, "live_version": version})
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("heartbeat answered %d", resp.StatusCode)
	}
	return nil
}

// PostTraffic reports per-host request counters read from the access log.
func (c *Client) PostTraffic(ctx context.Context, b traffic.Batch) error {
	resp, err := c.do(ctx, http.MethodPost, "/v1/gateways/"+url.PathEscape(c.Gateway)+"/traffic", b)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("traffic report answered %d", resp.StatusCode)
	}
	return nil
}

// Latest fetches the newest signed config.
func (c *Client) Latest(ctx context.Context) (signing.Envelope, error) {
	resp, err := c.do(ctx, http.MethodGet, "/v1/config/latest", nil)
	if err != nil {
		return signing.Envelope{}, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return signing.Envelope{}, ErrNoConfig
	case resp.StatusCode != http.StatusOK:
		return signing.Envelope{}, fmt.Errorf("latest config answered %d", resp.StatusCode)
	}
	var env signing.Envelope
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&env); err != nil {
		return signing.Envelope{}, err
	}
	return env, nil
}

// Loop runs the heartbeat and the pull loop.
type Loop struct {
	Client  *Client
	Apply   func(ctx context.Context, env signing.Envelope) (int64, error)
	Version func() int64
	State   func() string
	Logf    func(format string, args ...any)

	failedVersion int64
	failedSHA     string
}

func (l *Loop) logf(format string, args ...any) {
	if l.Logf != nil {
		l.Logf(format, args...)
	}
}

// PullOnce fetches the latest config and applies it if it is newer than what is live. A push
// that already landed makes this a no-op.
func (l *Loop) PullOnce(ctx context.Context) error {
	env, err := l.Client.Latest(ctx)
	if err != nil {
		if errors.Is(err, ErrNoConfig) {
			return nil
		}
		return err
	}
	if env.Version <= l.Version() {
		return nil
	}
	if env.Version == l.failedVersion && env.SHA256 == l.failedSHA {
		return nil // already tried and refused; wait for the control plane to move on
	}
	if _, err = l.Apply(trace.With(ctx, trace.NewID()), env); err != nil {
		l.failedVersion, l.failedSHA = env.Version, env.SHA256
	}
	return err
}

// Run heartbeats and pulls until ctx ends. Failures are logged and retried on the next tick: a
// gateway keeps serving its last config while the control plane is unreachable. Heartbeats have
// their own goroutine so a slow apply cannot make a healthy gateway look dead.
func (l *Loop) Run(ctx context.Context, heartbeatEvery, pullEvery time.Duration) {
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		hb := time.NewTicker(heartbeatEvery)
		defer hb.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-hb.C:
				if err := l.Client.Heartbeat(ctx, l.State(), l.Version()); err != nil {
					l.logf("heartbeat failed: %v", err)
				}
			}
		}
	}()
	defer wg.Wait()
	pull := time.NewTicker(pullEvery)
	defer pull.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-pull.C:
			if err := l.PullOnce(ctx); err != nil {
				l.logf("pull failed: %v", err)
			}
		}
	}
}

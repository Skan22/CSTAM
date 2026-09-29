// Package trace emits one span per pipeline stage, tagged with the trace id of the request that
// started it, so a registration can be followed from the API through the agent's swap.
//
// Spans go out as JSON lines; a collector can ship them to OpenTelemetry without the agent
// depending on an SDK.
package trace

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"sync"
	"time"
)

type ctxKey struct{}

// NewID returns a random 128-bit trace id in hex.
func NewID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// With returns ctx carrying the trace id.
func With(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, ctxKey{}, id)
}

// ID returns the trace id in ctx, or "".
func ID(ctx context.Context) string {
	id, _ := ctx.Value(ctxKey{}).(string)
	return id
}

// Span is one finished stage.
type Span struct {
	TraceID    string         `json:"trace_id"`
	Name       string         `json:"name"`
	Start      string         `json:"start"`
	DurationMS float64        `json:"duration_ms"`
	Error      string         `json:"error,omitempty"`
	Attrs      map[string]any `json:"attrs,omitempty"`
}

// Tracer records spans.
type Tracer struct {
	mu    sync.Mutex
	out   io.Writer
	spans []Span
	keep  bool
}

// New writes spans to out (nil to discard). keep retains them for inspection in tests.
func New(out io.Writer, keep bool) *Tracer { return &Tracer{out: out, keep: keep} }

// Start begins a span; call the returned function with the stage's error when it ends.
func (t *Tracer) Start(ctx context.Context, name string, attrs map[string]any) func(error) {
	start := time.Now()
	return func(err error) {
		s := Span{TraceID: ID(ctx), Name: name, Start: start.UTC().Format(time.RFC3339Nano),
			DurationMS: float64(time.Since(start).Microseconds()) / 1000, Attrs: attrs}
		if err != nil {
			s.Error = err.Error()
		}
		t.mu.Lock()
		defer t.mu.Unlock()
		if t.keep {
			t.spans = append(t.spans, s)
		}
		if t.out != nil {
			line, _ := json.Marshal(s)
			_, _ = t.out.Write(append(line, '\n'))
		}
	}
}

// Spans returns a copy of the retained spans.
func (t *Tracer) Spans() []Span {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]Span(nil), t.spans...)
}

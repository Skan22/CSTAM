// Package metrics renders the agent's Prometheus text exposition by hand, so the agent has no
// third-party dependency.
package metrics

import (
	"fmt"
	"io"
	"sort"
	"sync"
)

var buckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5}

// Registry holds the agent's metrics.
type Registry struct {
	mu          sync.Mutex
	version     int64
	rollbacks   int64
	transitions int64
	vrrp        string
	rejected    map[string]int64
	applied     int64
	counts      []int64
	sum         float64
	n           int64
}

func New() *Registry {
	return &Registry{rejected: map[string]int64{}, counts: make([]int64, len(buckets))}
}

func (r *Registry) SetVersion(v int64) { r.mu.Lock(); r.version = v; r.mu.Unlock() }
func (r *Registry) Rollback()          { r.mu.Lock(); r.rollbacks++; r.mu.Unlock() }
func (r *Registry) Applied()           { r.mu.Lock(); r.applied++; r.mu.Unlock() }
func (r *Registry) Rejected(reason string) {
	r.mu.Lock()
	r.rejected[reason]++
	r.mu.Unlock()
}

// VRRP records the current state and counts a transition when it changes.
func (r *Registry) VRRP(state string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.vrrp != "" && r.vrrp != state {
		r.transitions++
	}
	r.vrrp = state
}

// ObserveReload records how long a config took from receipt to verified.
func (r *Registry) ObserveReload(seconds float64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for i, b := range buckets {
		if seconds <= b {
			r.counts[i]++
		}
	}
	r.sum += seconds
	r.n++
}

// Snapshot is used by tests and /status.
func (r *Registry) Snapshot() (version, rollbacks, transitions int64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.version, r.rollbacks, r.transitions
}

// Write renders the exposition.
func (r *Registry) Write(w io.Writer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := func(format string, a ...any) { fmt.Fprintf(w, format+"\n", a...) }
	p("# TYPE ipo_agent_config_version gauge")
	p("ipo_agent_config_version %d", r.version)
	p("# TYPE ipo_agent_config_applied_total counter")
	p("ipo_agent_config_applied_total %d", r.applied)
	p("# TYPE ipo_agent_config_rejected_total counter")
	reasons := make([]string, 0, len(r.rejected))
	for k := range r.rejected {
		reasons = append(reasons, k)
	}
	sort.Strings(reasons)
	for _, k := range reasons {
		p("ipo_agent_config_rejected_total{reason=%q} %d", k, r.rejected[k])
	}
	p("# TYPE ipo_agent_rollbacks_total counter")
	p("ipo_agent_rollbacks_total %d", r.rollbacks)
	p("# TYPE ipo_agent_vrrp_transitions_total counter")
	p("ipo_agent_vrrp_transitions_total %d", r.transitions)
	p("# TYPE ipo_agent_reload_duration_seconds histogram")
	for i, b := range buckets {
		p("ipo_agent_reload_duration_seconds_bucket{le=\"%g\"} %d", b, r.counts[i])
	}
	p("ipo_agent_reload_duration_seconds_bucket{le=\"+Inf\"} %d", r.n)
	p("ipo_agent_reload_duration_seconds_sum %g", r.sum)
	p("ipo_agent_reload_duration_seconds_count %d", r.n)
}

// Package vrrp reads keepalived's state and injects the fault that forces a failover.
//
// keepalived's notify scripts write MASTER, BACKUP or FAULT to a state file on every transition.
// Forcing a failover writes a track file that a `track_file` block in keepalived.conf turns into
// a priority drop below the peer's, so the peer takes over within the advert interval.
package vrrp

import (
	"context"
	"os"
	"strings"
	"time"
)

var valid = map[string]bool{"MASTER": true, "BACKUP": true, "FAULT": true}

// Monitor watches the state file.
type Monitor struct {
	StateFile string
	FaultFile string
}

// State returns MASTER, BACKUP or FAULT, or UNKNOWN if the file is missing or unrecognised.
func (m Monitor) State() string {
	b, err := os.ReadFile(m.StateFile)
	if err != nil {
		return "UNKNOWN"
	}
	s := strings.ToUpper(strings.TrimSpace(string(b)))
	if !valid[s] {
		return "UNKNOWN"
	}
	return s
}

// Fault makes this node give up MASTER.
func (m Monitor) Fault() error {
	tmp := m.FaultFile + ".tmp"
	if err := os.WriteFile(tmp, []byte("-100\n"), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, m.FaultFile)
}

// Clear lifts a fault.
func (m Monitor) Clear() error {
	if err := os.Remove(m.FaultFile); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// Faulted reports whether a fault is being injected.
func (m Monitor) Faulted() bool {
	_, err := os.Stat(m.FaultFile)
	return err == nil
}

// Watch calls on with the state at start and again after each change, until ctx ends. A change
// is reported once two consecutive reads agree, so a notify script that truncates and rewrites
// the file (rather than renaming it into place) cannot produce a spurious UNKNOWN.
func (m Monitor) Watch(ctx context.Context, every time.Duration, on func(state string)) {
	last, candidate := "", ""
	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		s := m.State()
		if last == "" || s == candidate {
			if s != last {
				last = s
				on(s)
			}
		}
		candidate = s
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

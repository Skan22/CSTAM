package metrics

import (
	"strings"
	"testing"
)

func exposition(r *Registry) string {
	var b strings.Builder
	r.Write(&b)
	return b.String()
}

func TestVRRPMasterGaugeFollowsTheState(t *testing.T) {
	r := New()
	for state, want := range map[string]string{"MASTER": "1", "BACKUP": "0", "FAULT": "0", "UNKNOWN": "0"} {
		r.VRRP(state)
		if got := exposition(r); !strings.Contains(got, "\nipo_agent_vrrp_master "+want+"\n") {
			t.Errorf("state %s: want ipo_agent_vrrp_master %s in\n%s", state, want, got)
		}
	}
}

func TestTransitionsAreCountedOnlyOnChange(t *testing.T) {
	r := New()
	r.VRRP("BACKUP")
	r.VRRP("BACKUP")
	r.VRRP("MASTER")
	if _, _, n := r.Snapshot(); n != 1 {
		t.Fatalf("transitions = %d, want 1", n)
	}
}

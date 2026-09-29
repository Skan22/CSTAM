package vrrp

import (
	"context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestStateFile(t *testing.T) {
	dir := t.TempDir()
	m := Monitor{StateFile: filepath.Join(dir, "state"), FaultFile: filepath.Join(dir, "fault")}
	if m.State() != "UNKNOWN" {
		t.Error("missing file must be UNKNOWN")
	}
	for in, want := range map[string]string{"MASTER\n": "MASTER", " backup ": "BACKUP", "FAULT": "FAULT", "banana": "UNKNOWN"} {
		_ = os.WriteFile(m.StateFile, []byte(in), 0o644)
		if got := m.State(); got != want {
			t.Errorf("%q -> %s", in, got)
		}
	}
}

func TestFaultAndClear(t *testing.T) {
	dir := t.TempDir()
	m := Monitor{StateFile: filepath.Join(dir, "state"), FaultFile: filepath.Join(dir, "fault")}
	if m.Faulted() {
		t.Fatal("faulted at start")
	}
	if err := m.Fault(); err != nil || !m.Faulted() {
		t.Fatalf("fault: %v", err)
	}
	if err := m.Clear(); err != nil || m.Faulted() {
		t.Fatalf("clear: %v", err)
	}
	if err := m.Clear(); err != nil {
		t.Fatalf("clearing twice: %v", err)
	}
}

func TestWatchReportsTransitions(t *testing.T) {
	dir := t.TempDir()
	m := Monitor{StateFile: filepath.Join(dir, "state")}
	var mu sync.Mutex
	var seen []string
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		m.Watch(ctx, 2*time.Millisecond, func(s string) { mu.Lock(); seen = append(seen, s); mu.Unlock() })
		close(done)
	}()
	for { // the first report is the state at start, before any transition
		mu.Lock()
		n := len(seen)
		mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	for _, s := range []string{"BACKUP", "MASTER", "MASTER", "FAULT"} {
		_ = os.WriteFile(m.StateFile, []byte(s), 0o644)
		time.Sleep(15 * time.Millisecond)
	}
	cancel()
	<-done
	mu.Lock()
	defer mu.Unlock()
	want := []string{"UNKNOWN", "BACKUP", "MASTER", "FAULT"}
	if len(seen) != len(want) {
		t.Fatalf("seen %v", seen)
	}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("seen %v", seen)
		}
	}
}

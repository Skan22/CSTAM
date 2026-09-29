"""Failover under steady load, against real keepalived, Traefik and ipo-agent.

Every request opens a new connection through the VIP; a gap is the longest stretch in which none
succeeded. The targets are the plan's: the VIP moves within about 2 s when the MASTER machine
dies and within about 4 s when Traefik dies (two failed checks at 1 s, then the takeover).
"""

import json
import os
import signal
from pathlib import Path

import pytest

from chaos.world import World
from lab.loadgen import Report
from lab.stack import http_json, wait_for

VM_DEATH_TARGET = 2.0
TRAEFIK_CRASH_TARGET = 4.0
RUNS = int(os.environ.get("IPO_CHAOS_RUNS", "10"))


def record(name: str, report: Report) -> None:
    out = os.environ.get("IPO_LAB_WORKDIR")
    if out:
        with (Path(out).parent / "chaos-report.jsonl").open("a") as f:
            f.write(json.dumps({"scenario": name, **report.summary()}) + "\n")


def only_one_master(w: World) -> bool:
    return len(w.masters()) == 1 and len(w.vip_holders()) == 1


def test_steady_state_has_no_failures(pair: World) -> None:
    report = pair.measure(lambda: None, warmup=1, tail=3)
    assert report.failed == [] and report.slow == [], report.summary()
    assert len(report.summary()["vias"]) == 1  # type: ignore[arg-type]


def test_vm_death_moves_the_vip_within_target(pair: World) -> None:
    old = pair.master()
    survivor = pair.other(old)
    report = pair.measure(old.power_off)
    record("vm-death", report)
    assert report.worst_gap <= VM_DEATH_TARGET, report.summary()
    assert survivor.vrrp_state() == "MASTER" and survivor.has_vip()
    assert not old.has_vip()


@pytest.mark.parametrize("run", range(RUNS))
def test_vm_death_repeated(pair: World, run: int) -> None:
    old = pair.master()
    report = pair.measure(old.power_off, tail=5)
    record(f"vm-death-{run}", report)
    assert report.worst_gap <= VM_DEATH_TARGET, report.summary()


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM], ids=["sigkill", "sigterm"])
def test_traefik_crash_moves_the_vip_within_target(pair: World, sig: signal.Signals) -> None:
    old = pair.master()
    report = pair.measure(lambda: old.signal("traefik", sig), tail=8)
    record(f"traefik-{sig.name.lower()}", report)
    assert report.worst_gap <= TRAEFIK_CRASH_TARGET, report.summary()
    assert old.vrrp_state() == "FAULT"
    assert pair.other(old).vrrp_state() == "MASTER"


@pytest.mark.parametrize("run", range(RUNS))
def test_traefik_crash_repeated(pair: World, run: int) -> None:
    old = pair.master()
    report = pair.measure(lambda: old.signal("traefik", signal.SIGKILL), tail=7)
    record(f"traefik-crash-{run}", report)
    assert report.worst_gap <= TRAEFIK_CRASH_TARGET, report.summary()


def test_stopping_keepalived_hands_over_at_once(pair: World) -> None:
    old = pair.master()
    report = pair.measure(lambda: old.signal("keepalived", signal.SIGTERM), tail=4)
    record("keepalived-stop", report)
    assert report.worst_gap <= 1.0, report.summary()
    assert not old.has_vip() and pair.other(old).has_vip()


def test_old_master_returns_as_backup_without_a_flap(pair: World) -> None:
    old = pair.master()
    new = pair.other(old)
    pair.measure(old.power_off, tail=3)

    def bring_back() -> None:
        old.power_on()
        wait_for(lambda: old.vrrp_state() == "BACKUP", "the old master to settle as BACKUP", 20)

    report = pair.measure(bring_back, warmup=1, tail=4)
    record("old-master-returns", report)
    assert report.failed == [], report.summary()
    assert new.vrrp_state() == "MASTER" and new.has_vip()
    assert old.vrrp_state() == "BACKUP" and not old.has_vip()
    assert old.status()["live_version"] == new.status()["live_version"]


def test_manual_failover_through_the_agent(pair: World) -> None:
    old = pair.master()
    new = pair.other(old)
    def fault() -> None:
        code, _ = http_json("POST", f"{old.agent_url}/fault")
        assert code == 200
    report = pair.measure(fault, tail=4)
    record("agent-fault", report)
    assert report.worst_gap <= 1.5, report.summary()
    assert old.vrrp_state() == "FAULT" and new.vrrp_state() == "MASTER"
    assert old.status()["faulted"] is True

    code, _ = http_json("DELETE", f"{old.agent_url}/fault")
    assert code == 200
    wait_for(lambda: old.vrrp_state() == "BACKUP", "the cleared gateway to become BACKUP", 10)
    assert new.vrrp_state() == "MASTER" and new.has_vip() and not old.has_vip()
    assert old.status()["faulted"] is False


def test_a_partition_makes_two_masters_and_healing_it_leaves_one(pair: World) -> None:
    pair.partition()
    wait_for(lambda: len(pair.masters()) == 2, "both gateways to claim MASTER", 10)
    assert len(pair.vip_holders()) == 2
    # the control plane, which hears from both over the management network, must notice
    wait_for(lambda: pair.cp.split_brain(), "the control plane to report a split brain", 15)
    assert pair.cp.metric("ipo_gateway_masters") == 2
    pair.unpartition()
    wait_for(lambda: only_one_master(pair), "one MASTER to give up", 10)
    wait_for(lambda: not pair.cp.split_brain(), "the control plane to see it resolved", 15)
    assert pair.cp.metric("ipo_gateway_masters") == 1
    assert (pair.cp.metric("ipo_split_brain_total") or 0) >= 1

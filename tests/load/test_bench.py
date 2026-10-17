"""Measurements for the benchmark report: provisioning latency, rollback, throughput across 30
teams, and the gateways riding out a control-plane crash. Failover and burst numbers come from
the chaos suite and test_burst; `load.report` joins them all.

Every number here is a lab number: real keepalived, Traefik and ipo-agent behind the real control
plane, but a fake cloud (servers appear instantly) on one machine. The report says so.
"""

import asyncio
import os
import signal
import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest

from chaos.world import World
from lab.loadgen import LoadGen
from lab.stack import EDGE_CLIENT, wait_for
from load.burst import Burst, Registrar, fetch_through

Emit = Callable[[str, dict[str, Any]], None]
WARM = int(os.environ.get("IPO_BENCH_WARM", "50"))
COLD = int(os.environ.get("IPO_BENCH_COLD", "10"))
ROLLBACKS = int(os.environ.get("IPO_BENCH_ROLLBACKS", "20"))
OUTAGES = int(os.environ.get("IPO_BENCH_OUTAGES", "3"))


def registrar(w: World) -> Registrar:
    return Registrar(w.cp.api, fetch_through(w.vip, EDGE_CLIENT), w.plat["domain"])


def delete_all(w: World, prefix: str) -> None:
    for t in w.cp.api("GET", "/v1/teams")[1]:
        if t["slug"].startswith(prefix):
            w.cp.api("DELETE", f"/v1/teams/{t['id']}")
    wait_for(lambda: not [t for t in w.cp.api("GET", "/v1/teams")[1] if t["slug"].startswith(prefix)],
             "teams to be deleted", 180, 0.5)


def sequential(w: World, count: int, prefix: str) -> Burst:
    """One team at a time, each deleted before the next: the latency a lone user sees."""
    reg = registrar(w)
    results = []
    began = time.monotonic()
    for i in range(count):
        one = reg.run(1, f"{prefix}{i:02d}")
        results.extend(one.results)
        delete_all(w, f"{prefix}{i:02d}")
    return Burst(count, results, time.monotonic() - began)


def pool_settle(w: World, size: int) -> None:
    w.cp.api("PATCH", "/v1/settings", {"pool_target": size, "quarantine_seconds": 1})
    wait_for(lambda: w.cp.api("GET", "/v1/pool")[1]["size"] == size, f"a pool of {size}", 180, 0.5)


def test_cold_path_registration_latency(world: World, emit: Emit) -> None:
    """No warm VM to claim: the control plane reserves an address and boots one itself. With the
    lab's instant fake boot this is the platform's own overhead; the real cloud adds Nova's
    boot time, measured separately on FelCloud."""
    pool_settle(world, 0)
    burst = sequential(world, COLD, "cold")
    assert not burst.failures and {r.path for r in burst.results} == {"cold"}, burst.summary()
    emit("bench-cold", {"count": COLD, **burst.summary()})


def test_warm_path_registration_latency(world: World, emit: Emit) -> None:
    pool_settle(world, WARM)
    burst = sequential(world, WARM, "warm")
    assert not burst.failures and {r.path for r in burst.results} == {"warm"}, burst.summary()
    emit("bench-warm", {"count": WARM, **burst.summary()})


def top_version(w: World) -> int:
    return int(max(v["version"] for v in w.cp.api("GET", "/v1/config/versions")[1]["versions"]))


def gateways_run_beyond(w: World, version: int) -> bool:
    gws = w.cp.api("GET", "/v1/gateways")[1]["gateways"]
    return all((g["live_version"] or 0) > version for g in gws)


def wait_for_beyond(w: World, version: int, what: str, every: float = 0.05) -> None:
    wait_for(lambda: gateways_run_beyond(w, version), what, 30, every)


def test_config_rollback_reaches_both_gateways(world: World, emit: Emit) -> None:
    """Pin the previous config (an operator's rollback) and time it until both gateways run it,
    then unpin and time the return to the current state. A rolled-back config is re-issued under
    a new, higher version, so each leg waits for a version beyond the last."""
    pool_settle(world, 4)
    reg = registrar(world)
    assert not reg.run(1, "rb-base").failures
    timings: list[float] = []
    for i in range(ROLLBACKS):
        assert not reg.run(1, f"rb{i:02d}").failures
        versions = sorted(v["version"] for v in world.cp.api("GET", "/v1/config/versions")[1]["versions"])
        top = versions[-1]
        t0 = time.monotonic()
        code, body = world.cp.api("POST", "/v1/config/rollback", {"version": versions[-2]})
        assert code == 202, body
        wait_for_beyond(world, top, "the rollback to be live", every=0.02)
        timings.append(time.monotonic() - t0)
        code, body = world.cp.api("POST", "/v1/config/rollback", {"version": None})
        assert code == 202, body
        wait_for_beyond(world, top_version(world), "unpinning")
        delete_all(world, f"rb{i:02d}")
    delete_all(world, "rb-base")
    timings.sort()
    emit("bench-rollback", {
        "count": ROLLBACKS, "p50_s": round(statistics.median(timings), 3),
        "p95_s": round(timings[max(0, int(len(timings) * 0.95) - 1)], 3),
        "max_s": round(timings[-1], 3)})


@pytest.mark.parametrize("rate", [200, 500, 1000, 2000])
def test_gateway_throughput_across_thirty_teams(world: World, emit: Emit, rate: int) -> None:
    """30 teams' hosts spread over the VIP; every request opens a new connection."""
    pool_settle(world, 30)
    reg = registrar(world)
    if rate == 200:
        assert not reg.run(30, "tp").failures
    hosts = [f"tp-{i:02d}.{world.plat['domain']}" for i in range(30)]

    async def run() -> Any:
        gen = LoadGen(hosts, world.vip, rate=rate, timeout=2.0, slow_after=0.5, source=EDGE_CLIENT)
        gen.start()
        await asyncio.sleep(8)
        return await gen.stop()
    report = asyncio.run(run())
    ok = len(report.samples) - len(report.failed)
    emit(f"bench-throughput-{rate}", {
        "target_rps": rate, "teams": 30, "requests": len(report.samples), "failed": len(report.failed),
        "achieved_rps": round(ok / max(report.duration, 1e-9), 1),
        "p50_ms": round(report.percentile(0.5) * 1000, 1),
        "p95_ms": round(report.percentile(0.95) * 1000, 1),
        "p99_ms": round(report.percentile(0.99) * 1000, 1)})
    assert not report.failed, report.summary()
    if rate == 2000:
        delete_all(world, "tp-")


@pytest.mark.parametrize("run", range(OUTAGES))
def test_control_plane_crash_does_not_disturb_traffic(world: World, emit: Emit, run: int) -> None:
    """kill -9 the control plane under steady load and bring it back: serving continues on the
    last config the gateways verified, and the plane recovers its state from the database."""
    world.cp.api("PATCH", "/v1/settings", {"pool_target": 2})
    assert not registrar(world).run(1, f"co{run}").failures
    host = f"co{run}-00.{world.plat['domain']}"
    cp = world.cp

    def crash_and_restart() -> None:
        os.killpg(cp.proc.pid, signal.SIGKILL)
        cp.proc.wait(10)
        time.sleep(8)
        t0 = time.monotonic()
        cp.start()
        crash_and_restart.recovery = time.monotonic() - t0  # type: ignore[attr-defined]

    def measure() -> Any:
        async def go() -> Any:
            gen = LoadGen(host, world.vip, rate=100, timeout=1.0, source=EDGE_CLIENT)
            gen.start()
            await asyncio.sleep(1.5)
            await asyncio.get_running_loop().run_in_executor(None, crash_and_restart)
            await asyncio.sleep(3)
            return await gen.stop()
        return asyncio.run(go())
    report = measure()
    emit("bench-cp-crash", {
        "run": run, "requests": len(report.samples), "failed": len(report.failed),
        "outage_s": 8, "recovery_s": round(crash_and_restart.recovery, 2),  # type: ignore[attr-defined]
        "p99_ms": round(report.percentile(0.99) * 1000, 1)})
    assert not report.failed, report.summary()
    # the restarted plane still knows the team and can register new ones
    assert any(t["slug"] == f"co{run}-00" for t in cp.api("GET", "/v1/teams")[1])
    assert not registrar(world).run(1, f"after{run}").failures
    delete_all(world, f"co{run}")  # leave the lab as found: later tests count its addresses
    delete_all(world, f"after{run}")

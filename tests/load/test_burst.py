"""Simultaneous team registrations against the real gateway stack.

Each team must end with a distinct address, state `active`, and a subdomain that the gateway pair
serves from the right sandbox. The numbers go to `load-report.jsonl` for the benchmark report.
"""

import os
import time
from collections.abc import Callable

import pytest

from chaos.world import World
from lab.stack import EDGE_CLIENT, wait_for
from load.burst import Burst, Registrar, fetch_through

SIZES = [int(n) for n in os.environ.get("IPO_LOAD_SIZES", "10,20,30,50,90").split(",")]
ROUNDS = int(os.environ.get("IPO_LOAD_ROUNDS", "1"))
# Every sandbox answering within this long of the burst starting is the bar for a full burst.
BUDGET_S = float(os.environ.get("IPO_LOAD_BUDGET", "30"))


def registrar(w: World) -> Registrar:
    return Registrar(w.cp.api, fetch_through(w.vip, EDGE_CLIENT), w.plat["domain"])


def settle(w: World, pool: int) -> None:
    """Wait until the warm pool is exactly `pool` VMs and nothing is still being torn down or
    quarantined, so a burst is measured on a quiet platform and not against the last one's cleanup."""
    def quiet() -> bool:
        p = w.cp.api("GET", "/v1/pool")[1]
        return bool(p["size"] == pool and p["draining"] == 0 and p["quarantined"] == 0)
    wait_for(quiet, f"a quiet platform with {pool} warm VMs", 180, 0.5)
    time.sleep(1.0)


def clear(w: World, prefix: str) -> None:
    """Tear the burst's teams down and wait until their addresses are usable again."""
    _, teams = w.cp.api("GET", "/v1/teams")
    for t in teams:
        if t["slug"].startswith(prefix):
            w.cp.api("DELETE", f"/v1/teams/{t['id']}")
    wait_for(lambda: not [t for t in w.cp.api("GET", "/v1/teams")[1] if t["slug"].startswith(prefix)],
             "burst teams to be deleted", 120, 0.5)


def check(burst: Burst) -> None:
    assert burst.failures == [], burst.summary()
    assert burst.duplicate_ips == set(), burst.summary()
    assert len(set(burst.ips)) == burst.size


def test_burst_larger_than_the_warm_pool_still_completes(world: World,
                                                         record: Callable[..., None]) -> None:
    """More simultaneous teams than warm VMs: the surplus takes the cold path and must still end
    up active, on distinct addresses."""
    world.cp.api("PATCH", "/v1/settings", {"pool_target": 3, "quarantine_seconds": 1})
    settle(world, 3)
    burst = registrar(world).run(12, "cold")
    record("burst-12-over-pool-3", burst, {"pool_target": 3})
    check(burst)
    assert burst.summary()["paths"]["cold"] >= 5, burst.summary()
    clear(world, "cold")


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("rnd", range(ROUNDS))
def test_simultaneous_registrations(world: World, record: Callable[..., None], size: int,
                                    rnd: int) -> None:
    world.cp.api("PATCH", "/v1/settings", {"pool_target": size, "quarantine_seconds": 1})
    prefix = f"b{size}r{rnd}"
    settle(world, size)
    burst = registrar(world).run(size, prefix)
    record(f"burst-{size}", burst, {"round": rnd, "pool_target": size})
    check(burst)
    assert burst.wall <= BUDGET_S, burst.summary()
    clear(world, prefix)


def test_the_first_team_is_unaffected(world: World) -> None:
    """The team that predates every burst still answers once they are all gone."""
    status, body = fetch_through(world.vip, EDGE_CLIENT)(world.host)
    assert status == 200 and "sandbox ok" in body

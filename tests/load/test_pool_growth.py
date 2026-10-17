"""The warm pool follows demand: a burst larger than the pool leaves it sized for the next one,
and when the demand window passes the surplus is discarded."""

from chaos.world import World
from lab.stack import EDGE_CLIENT, wait_for
from load.burst import Registrar, fetch_through
from load.test_burst import settle

BURST = 16


def test_the_pool_grows_to_the_last_burst_and_shrinks_when_it_passes(world: World) -> None:
    w = world
    w.cp.api("PATCH", "/v1/settings", {"pool_target": 4, "quarantine_seconds": 1,
                                       "demand_window_seconds": 0})
    settle(w, 4)
    reg = Registrar(w.cp.api, fetch_through(w.vip, EDGE_CLIENT), w.plat["domain"])
    burst = reg.run(BURST, "grow")
    assert not burst.failures, burst.summary()
    assert burst.summary()["paths"]["cold"] >= BURST - 4 - 2  # the pool covered only part of it

    w.cp.api("PATCH", "/v1/settings", {"demand_window_seconds": 900})
    wait_for(lambda: w.cp.api("GET", "/v1/pool")[1]["size"] >= BURST,
             "the pool to grow to the burst it just saw", 60, 0.5)
    pool = w.cp.api("GET", "/v1/pool")[1]
    assert pool["target"] == 4 and pool["effective_target"] >= BURST
    # the next wave of the same size is served warm
    again = reg.run(BURST, "regrow")
    assert not again.failures and again.summary()["paths"]["cold"] == 0, again.summary()

    # when the window passes, the extra VMs go
    w.cp.api("PATCH", "/v1/settings", {"demand_window_seconds": 0})
    for t in w.cp.api("GET", "/v1/teams")[1]:
        if t["slug"].startswith(("grow-", "regrow-")):
            w.cp.api("DELETE", f"/v1/teams/{t['id']}")
    settle(w, 4)

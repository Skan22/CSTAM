"""The cleanup pipeline against the real gateway stack: leases that run out are purged from the
gateways, DNS, the cloud and the address pool, without anyone asking, and the address comes back.

Timings go to `load-report.jsonl` for the benchmark report.
"""

import os
import statistics
import time
from collections.abc import Callable
from typing import Any

from chaos.world import World
from lab.stack import EDGE_CLIENT, wait_for
from load.burst import Registrar, fetch_through
from load.test_burst import settle

TEAMS = int(os.environ.get("IPO_LIFECYCLE_TEAMS", "10"))
QUARANTINE = 5


def leases_by_state(w: World) -> dict[str, int]:
    return dict(w.cp.api("GET", "/v1/ipam")[1]["counts"])


def test_expired_teams_disappear_from_every_layer_and_their_addresses_return(
    world: World, sql: Callable[[str], str], emit: Callable[[str, dict[str, Any]], None],
) -> None:
    w = world
    w.cp.api("PATCH", "/v1/settings", {"pool_target": 0, "quarantine_seconds": QUARANTINE})
    # the last test's warm VMs must be gone first: an address they free during this test would be
    # lower than the expired ones and taken first, so the reuse check would see fresh addresses
    settle(w, 0)
    fetch = fetch_through(w.vip, EDGE_CLIENT)
    reg = Registrar(w.cp.api, fetch, w.plat["domain"])
    burst = reg.run(TEAMS, "exp")
    assert not burst.failures, burst.summary()
    hosts = [f"{r.slug}.{w.plat['domain']}" for r in burst.results]
    ips = {r.ip for r in burst.results}

    # alive in every layer
    assert all(fetch(h)[0] == 200 for h in hosts)
    assert all(w.dns_has(h) for h in hosts)
    before = leases_by_state(w)

    # the leases run out
    t0 = time.monotonic()
    sql("UPDATE leases SET expires_at = now() - interval '1 second' WHERE team_id IN"
        " (SELECT id FROM teams WHERE slug LIKE 'exp-%')")
    seen: dict[str, float] = {}
    freed: dict[str, float] = {}  # address -> seconds until it was free again

    def check() -> bool:
        now = time.monotonic() - t0
        if "route" not in seen and all(fetch(h)[0] == 404 for h in hosts):
            seen["route"] = now
        if "dns" not in seen and not any(w.dns_has(h) for h in hosts):
            seen["dns"] = now
        teams = [t for t in w.cp.api("GET", "/v1/teams?include_deleted=true")[1]
                 if t["slug"].startswith("exp-")]
        if "deleted" not in seen and all(t["state"] == "deleted" for t in teams):
            seen["deleted"] = now
        for lease in w.cp.api("GET", "/v1/ipam")[1]["leases"]:
            if lease["ip"] in ips and lease["state"] == "free":
                freed.setdefault(lease["ip"], now)
        if len(freed) == len(ips):
            seen["freed"] = max(freed.values())
        return "freed" in seen and "route" in seen and "dns" in seen

    wait_for(check, "every expired team to be purged", 120, 0.25)

    after = leases_by_state(w)
    emit("lifecycle-expiry", {
        "teams": TEAMS, "quarantine_s": QUARANTINE, **{f"{k}_s": round(v, 2) for k, v in seen.items()},
        "recycle_overhead_p50_s": round(statistics.median(freed.values()) - QUARANTINE, 2),
        "recycle_overhead_max_s": round(max(freed.values()) - QUARANTINE, 2),
        "leased_before": before.get("leased", 0), "leased_after": after.get("leased", 0)})
    assert after.get("leased", 0) == before.get("leased", 0) - TEAMS
    assert after.get("draining", 0) == 0 and after.get("quarantined", 0) == 0
    assert not any(fetch(h)[0] == 200 for h in hosts)
    # the addresses are reusable: a new burst is served by sandboxes at the very same addresses
    again = reg.run(TEAMS, "reuse")
    assert not again.failures, again.summary()
    assert ips & {r.ip for r in again.results}  # recycled, not just fresh ones
    sql("UPDATE leases SET expires_at = now() WHERE team_id IN"
        " (SELECT id FROM teams WHERE slug LIKE 'reuse-%')")

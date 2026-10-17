"""The cleanup pipeline against the real gateway stack: leases that run out are purged from the
gateways, DNS, the cloud and the address pool, without anyone asking, and the address comes back.

Timings go to `load-report.jsonl` for the benchmark report.
"""

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from chaos.world import World
from lab.stack import EDGE_CLIENT, wait_for
from load.burst import Registrar, fetch_through

TEAMS = int(os.environ.get("IPO_LIFECYCLE_TEAMS", "10"))
QUARANTINE = 5


def write(name: str, data: dict[str, object]) -> None:
    line = {"scenario": name, **data}
    print("\n" + json.dumps(line))
    out = os.environ.get("IPO_LAB_WORKDIR")
    if out:
        with (Path(out).parent / "load-report.jsonl").open("a") as f:
            f.write(json.dumps(line) + "\n")


def leases_by_state(w: World) -> dict[str, int]:
    return dict(w.cp.api("GET", "/v1/ipam")[1]["counts"])


def test_expired_teams_disappear_from_every_layer_and_their_addresses_return(
    world: World, sql: Callable[[str], str],
) -> None:
    w = world
    w.cp.api("PATCH", "/v1/settings", {"pool_target": 0, "quarantine_seconds": QUARANTINE})
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
        counts = leases_by_state(w)
        if "freed" not in seen and "deleted" in seen and counts.get("quarantined", 0) == 0:
            seen["freed"] = now
        return "freed" in seen and "route" in seen and "dns" in seen

    wait_for(check, "every expired team to be purged", 120, 0.25)

    after = leases_by_state(w)
    write("lifecycle-expiry", {
        "teams": TEAMS, "quarantine_s": QUARANTINE, **{f"{k}_s": round(v, 2) for k, v in seen.items()},
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

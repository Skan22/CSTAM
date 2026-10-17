"""One team cannot use up the gateways for the others: Traefik's rate limit is keyed by Host."""

import asyncio
from typing import Any

from chaos.world import World
from lab.loadgen import LoadGen
from lab.stack import EDGE_CLIENT
from load.burst import Registrar, fetch_through


def test_a_team_over_its_limit_is_refused_while_its_neighbour_is_unaffected(world: World) -> None:
    w = world
    reg = Registrar(w.cp.api, fetch_through(w.vip, EDGE_CLIENT), w.plat["domain"])
    assert not reg.run(2, "rl").failures
    hot, calm = (f"rl-{i:02d}.{w.plat['domain']}" for i in range(2))

    async def run() -> tuple[Any, Any]:
        loud = LoadGen(hot, w.vip, rate=1200, timeout=2.0, source=EDGE_CLIENT)
        quiet = LoadGen(calm, w.vip, rate=20, timeout=2.0, source=EDGE_CLIENT)
        loud.start()
        quiet.start()
        await asyncio.sleep(4)
        return await loud.stop(), await quiet.stop()

    loud, quiet = asyncio.run(run())
    refused = [s for s in loud.samples if s.error.startswith("HTTP/1.1 429")]
    served = [s for s in loud.samples if s.ok]
    # about 200 a second (plus a burst of 100) get through; the rest are told to slow down
    assert len(refused) > 0.5 * len(loud.samples), (len(refused), len(loud.samples))
    assert 4 * 200 * 0.5 < len(served) < 4 * 200 * 2 + 200, len(served)
    assert quiet.samples and all(s.ok for s in quiet.samples), quiet.summary()
    for t in w.cp.api("GET", "/v1/teams")[1]:
        if t["slug"].startswith("rl-"):
            w.cp.api("DELETE", f"/v1/teams/{t['id']}")

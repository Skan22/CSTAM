"""Boots the whole gateway stack inside a namespace lab: two gateways with real keepalived,
Traefik and ipo-agent, a sandbox backend, and the real control plane with a fake cloud."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab import netns, render
from lab.loadgen import LoadGen, Report
from lab.stack import (
    EDGE_CLIENT,
    MGMT_CLIENT,
    Backend,
    ControlPlane,
    Gateway,
    signing_key,
    wait_for,
)

SLUG = "chaos"


@dataclass
class World:
    lab: netns.Lab
    plat: dict[str, Any]
    gateways: dict[str, Gateway]
    cp: ControlPlane
    backend: Backend
    host: str
    timeline: list[tuple[float, str, str]] = field(default_factory=list)
    t0: float = field(default_factory=time.monotonic)

    @property
    def vip(self) -> str:
        return str(self.plat["network"]["vip"])

    def states(self) -> dict[str, str]:
        return {n: g.vrrp_state() for n, g in self.gateways.items()}

    def masters(self) -> list[str]:
        return [n for n, s in self.states().items() if s == "MASTER"]

    def vip_holders(self) -> list[str]:
        return [n for n, g in self.gateways.items() if g.has_vip()]

    def master(self) -> Gateway:
        [name] = self.masters()
        return self.gateways[name]

    def other(self, gw: Gateway) -> Gateway:
        [o] = [g for g in self.gateways.values() if g is not gw]
        return o

    def curl(self, timeout: float = 2) -> str:
        """One request for the team's host through the VIP; the body, or "" on any failure."""
        p = netns.run(["curl", "-s", "-m", str(timeout), "-H", f"Host: {self.host}",
                       f"http://{self.vip}/"], check=False)
        return p.stdout if p.returncode == 0 else ""

    def measure(self, action: Callable[[], None], *, warmup: float = 1.5, tail: float = 6.0,
                rate: float = 50, timeout: float = 1.0) -> Report:
        """Send steady traffic through the VIP, run `action` after `warmup`, keep sending for
        `tail` more seconds, and report every failed or slow request."""
        async def run() -> Report:
            gen = LoadGen(self.host, self.vip, rate=rate, timeout=timeout, source=EDGE_CLIENT)
            gen.start()
            await asyncio.sleep(warmup)
            mark = gen.now()
            await asyncio.get_running_loop().run_in_executor(None, action)
            self.timeline.append((mark, "action", getattr(action, "__name__", "action")))
            await asyncio.sleep(tail)
            return await gen.stop()
        return asyncio.run(run())

    def partition(self) -> None:
        """Drop VRRP (IP protocol 112) both ways on both gateways: they stay up and reachable by
        the control plane, but can no longer hear each other."""
        for g in self.gateways.values():
            netns.run(["nft", "-f", "-"], ns=g.ns, input=(
                "table inet split {\n"
                "  chain in { type filter hook input priority 0; ip protocol 112 drop; }\n"
                "  chain out { type filter hook output priority 0; ip protocol 112 drop; }\n"
                "}\n"))

    def unpartition(self) -> None:
        for g in self.gateways.values():
            netns.run(["nft", "delete", "table", "inet", "split"], ns=g.ns, check=False)

    def heal(self, timeout: float = 30) -> None:
        """Undo whatever a scenario broke and wait until the pair is a clean master and backup."""
        self.unpartition()
        for g in self.gateways.values():
            g.repair()
        def settled() -> bool:
            states = sorted(self.states().values())
            return (states == ["BACKUP", "MASTER"] and len(self.vip_holders()) == 1
                    and "sandbox ok" in self.curl(1))
        wait_for(settled, "a clean master and backup", timeout, 0.2)

    def shutdown(self) -> None:
        for g in self.gateways.values():
            g.kill_all()
        self.cp.stop()
        self.backend.proc.kill()

    def wait_one_master(self, timeout: float = 15) -> str:
        def one() -> str | None:
            m = self.masters()
            return m[0] if len(m) == 1 else None
        return str(wait_for(one, "exactly one MASTER", timeout, 0.05))


def boot(workdir: Path, dsn: str) -> World:
    plat = render.platform()
    lab = netns.Lab(workdir)
    lab.setup()
    lab.bridge("edge", f"{EDGE_CLIENT}/{plat['network']['edge_cidr'].split('/')[1]}")
    lab.bridge("sbx")
    lab.bridge("mgmt", f"{MGMT_CLIENT}/{plat['network']['mgmt_cidr'].split('/')[1]}")

    seed, public = signing_key()
    cp = ControlPlane(lab, plat, dsn, seed)
    gws = {n: Gateway(lab, plat, n, cp_url=cp.url, signing_public=public) for n in ("gw-a", "gw-b")}
    backend = Backend(lab, plat)
    backend.start()
    for g in gws.values():
        g.plug()
        g.render()
    cp.start()
    for g in gws.values():
        g.start()
    world = World(lab, plat, gws, cp, backend, f"{SLUG}.{plat['domain']}")
    world.wait_one_master()
    cp.register(SLUG)
    cp.wait_active(SLUG)
    return world

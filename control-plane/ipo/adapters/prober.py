"""Probes that a team's hostname answers through the gateway VIP."""

import time
from collections.abc import Mapping
from typing import Protocol

import httpx


class NullProber:
    """For development without a VIP: nothing to probe."""

    def probe(self, host: str, timeout: float) -> None:
        return None


class HttpProber:
    """Requests `host` through the VIP until the sandbox answers, or `timeout` runs out."""

    def __init__(self, vip: str, *, port: int = 80, scheme: str = "http",
                 interval: float = 0.5) -> None:
        self.base = f"{scheme}://{vip}:{port}/"
        self.interval = interval

    def probe(self, host: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last = "no attempt"
        while True:
            try:
                r = httpx.get(self.base, headers={"Host": host}, timeout=min(2.0, timeout),
                              follow_redirects=False)
                if r.status_code < 500:
                    return
                last = f"HTTP {r.status_code}"
            except httpx.HTTPError as exc:
                last = str(exc) or type(exc).__name__
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{host} did not answer through {self.base}: {last}")
            time.sleep(self.interval)


class ProbingAgent(Protocol):
    def probe(self, host: str) -> tuple[bool, str]: ...


class AgentProber:
    """Asks the gateway agents to request `host` through their own Traefik until one is answered
    by the team's sandbox. This is the path real traffic takes; the management network, where the
    control plane runs, has no route to the VIP (found on the first real deploy)."""

    def __init__(self, agents: Mapping[str, ProbingAgent], *, interval: float = 1.0) -> None:
        self.agents = agents
        self.interval = interval

    def probe(self, host: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last: dict[str, str] = {}
        while True:
            for name, agent in self.agents.items():
                ok, why = agent.probe(host)
                if ok:
                    return
                last[name] = why
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{host} did not answer through the gateways: {last}")
            time.sleep(self.interval)

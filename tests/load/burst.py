"""Registers N teams at the same instant and measures what each one experiences.

One thread per team, released together by a barrier, each doing what a real client does: POST
/v1/teams with its own Idempotency-Key, poll until the team is active, then request its subdomain
through the VIP until the sandbox answers. Per team that gives three latencies: the API's answer
(accept), the time until the control plane calls the team active, and the time until traffic
actually reaches the sandbox (routed). A burst passes only if all of them succeed, every team got
a different address and every sandbox answered with its own content.
"""

import http.client
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Api = Callable[..., tuple[int, Any]]
Fetch = Callable[[str], tuple[int, str]]


@dataclass
class TeamResult:
    slug: str
    accepted_at: float = 0.0        # seconds from the burst start until POST /v1/teams returned
    active_at: float = 0.0          # ... until the team reported state=active
    routed_at: float = 0.0          # ... until the VIP served the team's host with a 200
    status: int = 0
    path: str = ""
    ip: str = ""
    state: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.state == "active" and self.routed_at > 0


@dataclass
class Burst:
    size: int
    results: list[TeamResult]
    wall: float                     # the burst start until the last team served traffic

    @property
    def failures(self) -> list[TeamResult]:
        return [r for r in self.results if not r.ok]

    @property
    def ips(self) -> list[str]:
        return [r.ip for r in self.results if r.ip]

    @property
    def duplicate_ips(self) -> set[str]:
        return {ip for ip in self.ips if self.ips.count(ip) > 1}

    def column(self, name: str) -> list[float]:
        return sorted(getattr(r, name) for r in self.results if r.ok)

    def summary(self) -> dict[str, Any]:
        def stats(name: str) -> dict[str, float]:
            xs = self.column(name)
            if not xs:
                return {}
            return {"min": round(xs[0], 3), "p50": round(statistics.median(xs), 3),
                    "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 3),
                    "max": round(xs[-1], 3)}
        return {
            "size": self.size, "ok": self.size - len(self.failures), "failed": len(self.failures),
            "wall_s": round(self.wall, 3), "unique_ips": len(set(self.ips)),
            "duplicate_ips": sorted(self.duplicate_ips),
            "paths": {p: sum(1 for r in self.results if r.path == p) for p in ("warm", "cold")},
            "accept": stats("accepted_at"), "active": stats("active_at"), "routed": stats("routed_at"),
            "errors": sorted({r.error for r in self.results if r.error}),
        }


@dataclass
class Registrar:
    """Everything one burst needs from the platform under test."""

    api: Api
    fetch: Fetch                    # (host) -> (status, body) through the gateway VIP
    domain: str
    timeout: float = 120
    poll: float = 0.1

    def one(self, slug: str, t0: list[float], gate: threading.Barrier) -> TeamResult:
        r = TeamResult(slug)
        gate.wait()
        start = t0[0]
        try:
            code, body = self.api("POST", "/v1/teams", {"slug": slug},
                                  headers={"Idempotency-Key": f"burst-{slug}"})
            r.accepted_at, r.status = time.monotonic() - start, code
            if code != 202:
                r.error = f"POST /v1/teams -> {code} {body}"
                return r
            r.ip, r.path, team_id = body["ip"], body["path"], body["team_id"]
            deadline = start + self.timeout
            while time.monotonic() < deadline:
                code, team = self.api("GET", f"/v1/teams/{team_id}")
                r.state = team.get("state", "") if code == 200 else f"http {code}"
                if r.state == "active":
                    r.active_at = time.monotonic() - start
                    break
                if r.state in ("failed", "deleted"):
                    r.error = f"team ended {r.state}"
                    return r
                time.sleep(self.poll)
            else:
                r.error = f"not active after {self.timeout}s (state {r.state})"
                return r
            host = f"{slug}.{self.domain}"
            while time.monotonic() < deadline:
                status, text = self.fetch(host)
                if status == 200:
                    r.routed_at = time.monotonic() - start
                    if f"to={r.ip} " not in text and f"to={r.ip}\n" not in text:
                        r.error = f"routed to the wrong sandbox: {text.strip()!r}"
                    return r
                time.sleep(self.poll)
            r.error = "active but the gateway never served its host"
        except Exception as e:  # a worker thread must report, never die silently
            r.error = f"{type(e).__name__}: {e}"
        return r

    def run(self, size: int, prefix: str) -> Burst:
        slugs = [f"{prefix}-{i:02d}" for i in range(size)]
        gate = threading.Barrier(size + 1)
        results: dict[str, TeamResult] = {}
        t0 = [0.0]

        def work(slug: str) -> None:
            results[slug] = self.one(slug, t0, gate)

        threads = [threading.Thread(target=work, args=(s,), daemon=True) for s in slugs]
        for t in threads:
            t.start()
        start = t0[0] = time.monotonic()
        gate.wait()
        for t in threads:
            t.join(self.timeout + 30)
        return Burst(size, [results[s] for s in slugs if s in results], time.monotonic() - start)


def fetch_through(vip: str, source: str | None = None, timeout: float = 2) -> Fetch:
    """A fetch() that sends `Host: <host>` to the VIP, optionally from a given source address."""
    def fetch(host: str) -> tuple[int, str]:
        try:
            c = http.client.HTTPConnection(vip, 80, timeout=timeout,
                                           source_address=(source, 0) if source else None)
            c.request("GET", "/", headers={"Host": host, "Connection": "close"})
            resp = c.getresponse()
            body = resp.read().decode(errors="replace")
            c.close()
            return resp.status, body
        except OSError:
            return 0, ""
    return fetch

"""Config compiler: routes table -> deterministic Traefik dynamic config -> signed version.

`render` is a pure function of the routes, so the same state always yields the same bytes and
an unchanged state never yields a new version. `build_version` stores the signed result;
`Pusher` delivers it to both gateway agents; `CompilerController` is the debounced,
leader-elected loop that ties them to the `ipo_routes_changed` notification.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from ipo.domain import leases
from ipo.domain.signing import Signer, sha256_hex
from ipo.settings import Settings

log = logging.getLogger("ipo.compiler")

CHANNEL = "ipo_routes_changed"
PIN_KEY = "config.pin"
CANARY_BACKEND = "http://127.0.0.1:8082"  # answered by the gateway agent itself

MIDDLEWARES: dict[str, dict[str, Any]] = {
    "secure-headers": {"headers": {
        "stsSeconds": 31536000, "contentTypeNosniff": True, "frameDeny": True}},
    "rate-limit": {"rateLimit": {"average": 100, "burst": 50}},
    "compress": {"compress": {}},
}


@dataclass(frozen=True)
class RouteRow:
    host: str
    backend_ip: str
    backend_port: int
    middlewares: Sequence[str]


@dataclass(frozen=True)
class ConfigVersion:
    version: int
    sha256: str
    signature: str
    body: str


def _name(host: str) -> str:
    return "t-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


def render(routes: Sequence[RouteRow], *, domain: str) -> str:
    """Traefik dynamic configuration for `routes`, byte-identical for the same set."""
    routers: dict[str, Any] = {
        "ipo-canary": {"rule": f"Host(`canary.{domain}`)", "service": "ipo-canary",
                       "entryPoints": ["web"]},
    }
    services: dict[str, Any] = {
        "ipo-canary": {"loadBalancer": {"servers": [{"url": CANARY_BACKEND}]}},
    }
    used: set[str] = set()
    for r in sorted(routes, key=lambda r: r.host):
        unknown = sorted(set(r.middlewares) - MIDDLEWARES.keys())
        if unknown:
            raise ValueError(f"unknown middleware(s) for {r.host}: {', '.join(unknown)}")
        name = _name(r.host)
        router: dict[str, Any] = {"rule": f"Host(`{r.host}`)", "service": name,
                                  "entryPoints": ["web"]}
        if r.middlewares:
            router["middlewares"] = list(r.middlewares)
            used.update(r.middlewares)
        routers[name] = router
        services[name] = {"loadBalancer": {
            "servers": [{"url": f"http://{r.backend_ip}:{r.backend_port}"}]}}
    http: dict[str, Any] = {"routers": routers, "services": services}
    if used:
        http["middlewares"] = {m: MIDDLEWARES[m] for m in sorted(used)}
    return json.dumps({"http": http}, sort_keys=True, separators=(",", ":"))


def _load_routes(conn: psycopg.Connection) -> list[RouteRow]:
    return [RouteRow(h, ip, port, mw) for h, ip, port, mw in conn.execute(
        "SELECT host, host(backend_ip), backend_port, middlewares FROM routes")]


def _pin(conn: psycopg.Connection) -> int | None:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (PIN_KEY,)).fetchone()
    return None if not row or row[0].get("version") is None else int(row[0]["version"])


def build_version(
    conn: psycopg.Connection, signer: Signer, *, domain: str, trace_id: str | None = None
) -> ConfigVersion | None:
    """Store a new signed version if the desired config differs from the latest one.

    The same advisory lock the table trigger takes serialises builders, so the version we
    sign is exactly the one the trigger assigns.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('ipo.config_versions'))")
        pin = _pin(conn)
        if pin is None:
            body = render(_load_routes(conn), domain=domain)
        else:
            row = conn.execute("SELECT body FROM config_versions WHERE version = %s",
                               (pin,)).fetchone()
            if not row:
                raise LookupError(f"pinned config version {pin} no longer exists")
            body = row[0]
        digest = sha256_hex(body)
        latest = conn.execute(
            "SELECT version, sha256 FROM config_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if latest and latest[1] == digest:
            return None
        version = (latest[0] if latest else 0) + 1
        signature = signer.sign(version, digest, body)
        row = conn.execute(
            "INSERT INTO config_versions (version, sha256, signature, body)"
            " VALUES (%s, %s, %s, %s) RETURNING version", (version, digest, signature, body),
        ).fetchone()
        assert row and row[0] == version
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, detail)"
            " VALUES ('compiler', 'config.build', %s, %s)",
            (str(version), json.dumps({"sha256": digest, "trace_id": trace_id,
                                       "pinned": pin is not None})),
        )
    return ConfigVersion(version, digest, signature, body)


def rollback(conn: psycopg.Connection, version: int | None) -> None:
    """Pin the desired config to an earlier version's body (None unpins). The compiler then
    re-issues that body under a new, higher version number, so agents never see time run
    backwards."""
    with conn.transaction():
        if version is not None and not conn.execute(
            "SELECT 1 FROM config_versions WHERE version = %s", (version,)
        ).fetchone():
            raise LookupError(f"config version {version} does not exist")
        conn.execute(
            "INSERT INTO settings (key, value, changed_by) VALUES (%s, %s, 'rollback')"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, changed_by = 'rollback'",
            (PIN_KEY, json.dumps({"version": version})),
        )
        conn.execute("SELECT pg_notify(%s, 'rollback')", (CHANNEL,))


# ------------------------------------------------------------------- push
@dataclass(frozen=True)
class Ack:
    ok: bool
    live_version: int
    error: str | None = None


class Agent(Protocol):
    def put_config(self, envelope: ConfigVersion) -> Ack:
        """Deliver a version. Raise OSError/TimeoutError for transport failures (retried);
        return `Ack(ok=False)` when the agent rejects the config (never retried)."""
        ...


class Pusher:
    def __init__(
        self,
        agents: Mapping[str, Agent],
        connect: Callable[[], psycopg.Connection],
        *,
        max_attempts: int = 5,
        base_backoff: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.agents = agents
        self.connect = connect
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.sleep = sleep

    def _push_one(self, name: str, agent: Agent, v: ConfigVersion) -> str:
        for attempt in range(self.max_attempts):
            try:
                ack = agent.put_config(v)
            except (OSError, TimeoutError) as exc:
                log.warning("push v%s to %s failed (attempt %d): %s", v.version, name,
                            attempt + 1, exc)
                if attempt + 1 < self.max_attempts:
                    self.sleep(self.base_backoff * 2**attempt)
                continue
            if not ack.ok:
                log.error("gateway %s rejected v%s: %s", name, v.version, ack.error)
                return "rejected"
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO gateway_status (gateway, live_version, last_heartbeat)"
                    " VALUES (%s, %s, now()) ON CONFLICT (gateway) DO UPDATE SET"
                    " live_version = GREATEST(gateway_status.live_version, EXCLUDED.live_version),"
                    " last_heartbeat = now()", (name, v.version))
            return "ok"
        return "unreachable"

    def push(self, v: ConfigVersion) -> dict[str, str]:
        with ThreadPoolExecutor(max_workers=max(1, len(self.agents))) as pool:
            futures = {n: pool.submit(self._push_one, n, a, v) for n, a in self.agents.items()}
            result = {n: f.result() for n, f in futures.items()}
        outcomes = set(result.values())
        with self.connect() as conn, conn.transaction():
            if "rejected" in outcomes:
                conn.execute("UPDATE config_versions SET status = 'rejected'"
                             " WHERE version = %s AND status = 'pending'", (v.version,))
            elif outcomes == {"ok"}:
                conn.execute("UPDATE config_versions SET status = 'live' WHERE version = %s"
                             " AND status = 'pending'", (v.version,))
                conn.execute("UPDATE config_versions SET status = 'superseded'"
                             " WHERE status = 'live' AND version < %s", (v.version,))
        return result


class HttpAgent:
    """Client for a gateway agent's mTLS `PUT /config`."""

    def __init__(self, base_url: str, *, cert: tuple[str, str] | None = None,
                 ca: str | None = None, timeout: float = 5.0) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(base_url=base_url, cert=cert, verify=ca or True,
                                    timeout=timeout)

    def put_config(self, envelope: ConfigVersion) -> Ack:
        try:
            r = self._client.put("/config", json={
                "version": envelope.version, "sha256": envelope.sha256,
                "signature": envelope.signature, "body": envelope.body})
        except self._httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        if r.status_code >= 500:
            raise ConnectionError(f"agent answered {r.status_code}")
        data = r.json() if r.content else {}
        if r.status_code == 200:
            return Ack(True, int(data.get("live_version", envelope.version)))
        return Ack(False, int(data.get("live_version", 0)), data.get("error", r.text))

    def fault(self) -> None:
        """Ask the agent to stop keepalived's VRRP advertisements, forcing a failover."""
        try:
            r = self._client.post("/fault")
        except self._httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        if r.status_code != 200:
            raise ConnectionError(f"agent answered {r.status_code} to /fault")


# ------------------------------------------------------------ convergence
class DbGateways:
    """`Gateways` for the registration saga: a route has converged once every gateway's
    acked live version contains it."""

    def __init__(self, settings: Settings, *, poll_interval: float = 0.25) -> None:
        self.settings = settings
        self.poll_interval = poll_interval

    def _converged(self, conn: psycopg.Connection, host: str) -> bool:
        row = conn.execute(
            "SELECT count(*) FROM gateway_status g JOIN config_versions c"
            " ON c.version = g.live_version"
            " WHERE g.gateway = ANY(%s) AND position(%s in c.body) > 0",
            (list(self.settings.gateways), f"Host(`{host}`)"),
        ).fetchone()
        return bool(row and row[0] == len(self.settings.gateways))

    def _removed(self, conn: psycopg.Connection, host: str) -> bool:
        row = conn.execute(
            "SELECT count(*) FROM gateway_status g JOIN config_versions c"
            " ON c.version = g.live_version"
            " WHERE g.gateway = ANY(%s) AND position(%s in c.body) = 0",
            (list(self.settings.gateways), f"Host(`{host}`)"),
        ).fetchone()
        return bool(row and row[0] == len(self.settings.gateways))

    def _wait(self, check: Callable[[], bool], what: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{what} not acked by all gateways after {timeout}s")
            time.sleep(self.poll_interval)

    def wait_for_route(self, conn: psycopg.Connection, host: str, timeout: float) -> None:
        self._wait(lambda: self._converged(conn, host), f"route {host}", timeout)

    def wait_for_route_removed(self, conn: psycopg.Connection, host: str, timeout: float) -> None:
        self._wait(lambda: self._removed(conn, host), f"removal of {host}", timeout)


# ------------------------------------------------------------- controller
class CompilerController:
    """Leader-elected, debounced: a burst of route changes collapses into one version."""

    def __init__(
        self,
        connect: Callable[[], psycopg.Connection],
        signer: Signer,
        settings: Settings,
        *,
        push: Callable[[ConfigVersion], object],
        max_wait: float = 1.0,
    ) -> None:
        self.connect = connect
        self.signer = signer
        self.settings = settings
        self.push = push
        self.max_wait = max_wait

    def compile_once(self) -> ConfigVersion | None:
        with self.connect() as conn:
            v = build_version(conn, self.signer, domain=self.settings.domain)
        if v:
            try:
                self.push(v)
            except Exception:
                log.exception("push of config v%s failed", v.version)
        return v

    def run(self, stop: threading.Event) -> None:
        lead = self.connect()
        try:
            while not stop.is_set() and not leases.try_lead(lead, "compiler"):
                stop.wait(0.2)
            if stop.is_set():
                return
            lead.execute(f"LISTEN {CHANNEL}")
            self._safe_compile()  # catch up on anything missed while nobody led
            pending_since: float | None = None
            while not stop.is_set():
                quiet = self.settings.debounce_seconds
                got = next(iter(lead.notifies(timeout=quiet if pending_since else 0.2,
                                              stop_after=1)), None)
                now = time.monotonic()
                if got is not None:
                    pending_since = pending_since or now
                    if now - pending_since < self.max_wait:
                        continue
                elif pending_since is None:
                    continue
                pending_since = None
                self._safe_compile()
        finally:
            lead.close()

    def _safe_compile(self) -> None:
        try:
            self.compile_once()
        except Exception:
            log.exception("config compile failed")

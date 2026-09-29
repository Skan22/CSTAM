"""The team registration saga: claim, provision, personalize, route, converge, probe, activate.

Step 1 (claim) already ran inside the API request, in the same transaction that created the team
and the job, so here it only contributes its undo. `provision` is the cold path's boot and is a
no-op for a warm claim, whose VM is already running.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from ipo import metrics
from ipo.adapters.openstack.base import TAG, Cloud, port_name, server_name
from ipo.controllers.saga import Hook, Job, SagaRunner, Step
from ipo.domain import events, leases
from ipo.domain.registration import KIND
from ipo.domain.teardown import release_resources
from ipo.settings import Settings

STEP_NAMES = ["claim", "provision", "personalize", "route", "converge", "probe", "activate"]
ROUTES_CHANNEL = "ipo_routes_changed"


class Gateways(Protocol):
    def wait_for_route(self, conn: psycopg.Connection, host: str, timeout: float) -> None:
        """Return once both gateways have acked a version containing `host`; else raise."""
        ...

    def wait_for_route_removed(self, conn: psycopg.Connection, host: str, timeout: float) -> None:
        """Return once both gateways have acked a version without `host`; else raise."""
        ...


class Prober(Protocol):
    def probe(self, host: str, timeout: float) -> None:
        """Request `host` through the VIP until it answers; raise if it never does."""
        ...


@dataclass(frozen=True)
class Deps:
    cloud: Cloud
    gateways: Gateways
    prober: Prober
    settings: Settings
    converge_timeout: float = 30.0
    probe_timeout: float = 30.0


def _notify_routes(conn: psycopg.Connection, host: str) -> None:
    conn.execute("SELECT pg_notify(%s, %s)", (ROUTES_CHANNEL, host))


class Claim:
    name = "claim"

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        return None  # done by register_team, in the request's transaction

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        ip, team_id = job.payload["ip"], job.team_id
        lease = leases.get(conn, ip)
        if lease.team_id == team_id and lease.state == "leased" and job.payload["path"] == "warm":
            leases.return_to_pool(conn, ip)  # the VM is untouched and goes back to the pool
        elif job.payload["path"] == "cold" or lease.state == "draining":
            release_resources(conn, self.d.cloud, ip,
                              quarantine_seconds=self.d.settings.quarantine_seconds)
        conn.execute("UPDATE teams SET state = 'failed' WHERE id = %s", (team_id,))
        events.emit(conn, "team.failed", team_id=team_id, slug=job.payload["slug"])


class Provision:
    name = "provision"

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        ip = job.payload["ip"]
        lease = leases.get(conn, ip)
        if lease.server_id:
            return {"skipped": "already provisioned"}
        port = self.d.cloud.ensure_port(name=port_name(ip), ip=ip, tags=(TAG,))
        server = self.d.cloud.ensure_server(
            name=server_name(ip), port_id=port.id, tags=(TAG,), metadata={"ipo.role": "sandbox"})
        leases.attach_resources(conn, ip, port_id=port.id, server_id=server.id)
        return {"port_id": port.id, "server_id": server.id}

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        return None  # Claim.undo releases cold resources


class Personalize:
    name = "personalize"
    KEYS = ("ipo.team", "ipo.slug")

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        lease = leases.get(conn, job.payload["ip"])
        assert lease.server_id, "personalize runs after provision"
        self.d.cloud.set_server_metadata(
            lease.server_id, {"ipo.team": job.team_id or "", "ipo.slug": job.payload["slug"]})
        return {"server_id": lease.server_id}

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        lease = leases.get(conn, job.payload["ip"])
        if lease.server_id:
            self.d.cloud.clear_server_metadata(lease.server_id, self.KEYS)


class Route:
    name = "route"

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        host = job.payload["host"]
        with conn.transaction():
            conn.execute(
                "INSERT INTO routes (host, team_id, backend_ip, backend_port)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (host) DO NOTHING",
                (host, job.team_id, job.payload["ip"], self.d.settings.backend_port))
            row = conn.execute(
                "SELECT team_id::text FROM routes WHERE host = %s", (host,)
            ).fetchone()
            if not row or row[0] != job.team_id:
                raise RuntimeError(f"host {host} is routed for another team")
            _notify_routes(conn, host)
        return {"host": host}

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        with conn.transaction():
            conn.execute("DELETE FROM routes WHERE host = %s AND team_id = %s",
                         (job.payload["host"], job.team_id))
            _notify_routes(conn, job.payload["host"])


class Converge:
    name = "converge"

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        self.d.gateways.wait_for_route(conn, job.payload["host"], self.d.converge_timeout)
        return None

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        return None  # Route.undo removes the route and the compiler pushes the removal


class Probe:
    name = "probe"

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        self.d.prober.probe(job.payload["host"], self.d.probe_timeout)
        return None

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        return None


class Activate:
    name = "activate"

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        with conn.transaction():
            conn.execute("UPDATE teams SET state = 'active' WHERE id = %s AND state = 'pending'",
                         (job.team_id,))
            events.emit(conn, "team.active", team_id=job.team_id, slug=job.payload["slug"],
                        ip=job.payload["ip"], host=job.payload["host"])
            age = conn.execute(
                "SELECT extract(epoch FROM clock_timestamp() - created_at) FROM jobs"
                " WHERE id = %s", (job.id,)).fetchone()
        if age:
            metrics.PROVISION_DURATION.labels(job.payload["path"]).observe(float(age[0]))
        return None

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        return None


def build_runner(
    connect: Callable[[], psycopg.Connection], deps: Deps, *, worker_id: str,
    hook: Hook | None = None, extra: Mapping[str, Sequence[Step]] | None = None, **kwargs: Any,
) -> SagaRunner:
    steps: list[Step] = [
        Claim(deps), Provision(deps), Personalize(deps), Route(deps), Converge(deps),
        Probe(deps), Activate(deps),
    ]
    assert [s.name for s in steps] == STEP_NAMES
    return SagaRunner(connect, {KIND: steps, **(extra or {})}, worker_id=worker_id, hook=hook,
                      **kwargs)

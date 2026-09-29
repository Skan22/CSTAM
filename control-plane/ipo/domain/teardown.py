"""Tear a team down: stop routing, delete its VM and port, quarantine its address.

The saga steps, the reaper and the reconciler all call the same functions here. Every one is
safe to repeat, so a crash halfway is finished by simply calling it again.
"""

import json

import psycopg

from ipo.adapters.openstack.base import Cloud, port_name, server_name
from ipo.domain import events, leases
from ipo.domain.registration import KIND as REGISTER_KIND

KIND = "teardown"
ROUTES_CHANNEL = "ipo_routes_changed"


class TeamBusy(Exception):
    """The team is still being registered; let that saga finish or fail first."""


def audit(conn: psycopg.Connection, actor: str, action: str, target: str, **detail: object) -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, action, target, detail) VALUES (%s, %s, %s, %s)",
        (actor, action, target, json.dumps(detail)))


def enqueue_teardown(
    conn: psycopg.Connection, team_id: str, *, reason: str, actor: str
) -> str | None:
    """Queue the teardown saga for a team and mark it draining. Returns the job id, the id of
    the teardown already in flight, or None if the team is already deleted."""
    with conn.transaction():
        team = conn.execute(
            "SELECT slug, subdomain, state FROM teams WHERE id = %s FOR UPDATE", (team_id,)
        ).fetchone()
        if team is None:
            raise LookupError(f"team {team_id} does not exist")
        slug, host, state = team
        if state == "deleted":
            return None
        live = conn.execute(
            "SELECT id::text FROM jobs WHERE kind = %s AND team_id = %s"
            " AND state IN ('queued', 'running')", (KIND, team_id)).fetchone()
        if live:
            return str(live[0])
        if state == "pending" and conn.execute(
            "SELECT 1 FROM jobs WHERE kind = %s AND team_id = %s"
            " AND state IN ('queued', 'running')", (REGISTER_KIND, team_id)
        ).fetchone():
            raise TeamBusy(f"team {slug} is still being registered")
        row = conn.execute(
            "SELECT host(ip) FROM leases WHERE team_id = %s AND state IN ('leased', 'draining')",
            (team_id,)).fetchone()
        (n,) = conn.execute(
            "SELECT count(*) FROM jobs WHERE kind = %s AND team_id = %s", (KIND, team_id)
        ).fetchone() or (0,)
        job = conn.execute(
            "INSERT INTO jobs (kind, payload, team_id, idempotency_key)"
            " VALUES (%s, %s, %s, %s) RETURNING id::text",
            (KIND, json.dumps({"slug": slug, "host": host, "ip": row[0] if row else None,
                               "reason": reason}), team_id, f"{KIND}:{team_id}:{n}"),
        ).fetchone()
        assert job
        conn.execute("UPDATE teams SET state = 'draining' WHERE id = %s", (team_id,))
        audit(conn, actor, "team.teardown_started", slug, reason=reason, job_id=job[0])
        events.emit(conn, "team.draining", team_id=team_id, slug=slug, reason=reason)
    return str(job[0])


def drain_team(conn: psycopg.Connection, team_id: str) -> None:
    """Step 1: mark the team draining and remove its route so the compiler stops serving it."""
    with conn.transaction():
        conn.execute("UPDATE teams SET state = 'draining'"
                     " WHERE id = %s AND state IN ('active', 'pending', 'failed')", (team_id,))
        for (host,) in conn.execute(
            "DELETE FROM routes WHERE team_id = %s RETURNING host", (team_id,)
        ).fetchall():
            conn.execute("SELECT pg_notify(%s, %s)", (ROUTES_CHANNEL, host))


def release_team(
    conn: psycopg.Connection, cloud: Cloud, team_id: str, ip: str | None, *,
    quarantine_seconds: float,
) -> None:
    """Steps 3-4: delete the VM then the port, and quarantine the address. Does nothing if the
    address has meanwhile been released, or moved on to another team."""
    if ip is None:
        return
    lease = leases.get(conn, ip)
    if lease.team_id is not None and lease.team_id != team_id:
        return
    release_resources(conn, cloud, ip, quarantine_seconds=quarantine_seconds)


def finalize_team(conn: psycopg.Connection, team_id: str, *, actor: str = "teardown") -> None:
    """Step 5: the team is gone; record it and tell the dashboard."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE teams SET state = 'deleted' WHERE id = %s AND state <> 'deleted'"
            " RETURNING slug", (team_id,)).fetchone()
        if row:
            audit(conn, actor, "team.deleted", row[0])
            events.emit(conn, "team.deleted", team_id=team_id, slug=row[0])


def release_resources(
    conn: psycopg.Connection, cloud: Cloud, ip: str, *, quarantine_seconds: float
) -> leases.Lease:
    lease = leases.get(conn, ip)
    if lease.state in ("free", "quarantined"):
        return lease
    if lease.state != "draining":
        lease = leases.begin_drain(conn, ip)
    # Delete by recorded id and by deterministic name: a crash between creating a resource and
    # recording it must not leave an orphan behind.
    for server in cloud.list_servers():
        if server.id == lease.server_id or server.name == server_name(ip):
            cloud.delete_server(server.id)
    for port in cloud.list_ports():
        if port.id == lease.port_id or port.name == port_name(ip):
            cloud.delete_port(port.id)
    return leases.quarantine(conn, ip, seconds=quarantine_seconds)

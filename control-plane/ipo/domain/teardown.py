"""Tear a team down: stop routing, delete its VM and port, quarantine its address.

The saga steps, the reaper and the reconciler all call the same functions here. Every one is
safe to repeat, so a crash halfway is finished by simply calling it again.
"""

import json
import time
from collections.abc import Callable

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
    quarantine_seconds: float, verify_wait: float = 0.0,
) -> None:
    """Delete the VM then the port, quarantine the address, and check nothing was left. Does
    nothing if the address has meanwhile moved on to another team or back to the pool. Repeating
    it after a partial release (the lease already quarantined) deletes whatever is left."""
    if ip is None:
        return
    lease = leases.get(conn, ip)
    if lease.team_id is not None and lease.team_id != team_id:
        return
    if lease.state in ("free", "pooled"):
        return  # nothing of ours; a pooled lease is the warm pool's now
    if lease.state == "quarantined":
        _delete_resources(cloud, ip, lease)
    else:
        release_resources(conn, cloud, ip, quarantine_seconds=quarantine_seconds)
    verify_released(conn, cloud, ip, wait=verify_wait)


class NotSanitized(Exception):
    """An address was released but something of its old tenant is still around."""


def _leftovers(conn: psycopg.Connection, cloud: Cloud, ip: str) -> list[str]:
    found = [f"server {s.id}" for s in cloud.list_servers() if s.name == server_name(ip)]
    found += [f"port {p.id}" for p in cloud.list_ports() if p.ip == ip or p.name == port_name(ip)]
    lease = leases.get(conn, ip)
    if lease.state not in ("quarantined", "free"):
        found.append(f"lease {lease.state}")
    if lease.team_id or lease.server_id or lease.port_id:
        found.append("lease still linked to its old team or VM")
    return found


def verify_released(conn: psycopg.Connection, cloud: Cloud, ip: str, *, wait: float = 0.0,
                    interval: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> None:
    """After a release: no VM or port is left on `ip`, and its lease carries nothing of the old
    team. Raising makes the saga step retry, so an address is never recycled half-cleaned.

    Clouds delete asynchronously: Nova answers the delete at once and keeps listing the VM for
    seconds. `wait` is how long to keep looking before calling that a leftover."""
    deadline = time.monotonic() + wait
    leftovers = _leftovers(conn, cloud, ip)
    while leftovers and time.monotonic() < deadline:
        sleep(interval)
        leftovers = _leftovers(conn, cloud, ip)
    if leftovers:
        raise NotSanitized(f"{ip} was released but still has: {', '.join(leftovers)}")


def purge_team_data(conn: psycopg.Connection, team_id: str, host: str) -> dict[str, int]:
    """Remove what a deleted team could still be identified or reached by: its logins, its route
    (already gone after drain, but cheap to be sure) and the traffic recorded for its host. The
    team row itself and the audit trail stay: the name is never reused, and the trail is a
    hash chain that must not lose links."""
    users = conn.execute("DELETE FROM users WHERE team_id = %s", (team_id,)).rowcount
    routes = conn.execute("DELETE FROM routes WHERE team_id = %s", (team_id,)).rowcount
    buckets = conn.execute("DELETE FROM traffic_buckets WHERE host = %s", (host,)).rowcount
    recent = conn.execute("DELETE FROM traffic_recent WHERE host = %s", (host,)).rowcount
    return {"users": users, "routes": routes, "traffic_buckets": buckets, "traffic_recent": recent}


def finalize_team(conn: psycopg.Connection, team_id: str, *, actor: str = "teardown") -> None:
    """The last step: the team is gone. Purge what identified it, record it, tell the dashboard."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE teams SET state = 'deleted' WHERE id = %s AND state <> 'deleted'"
            " RETURNING slug, subdomain", (team_id,)).fetchone()
        if row:
            purged = purge_team_data(conn, team_id, row[1])
            audit(conn, actor, "team.deleted", row[0], purged=purged)
            events.emit(conn, "team.deleted", team_id=team_id, slug=row[0])


def release_resources(
    conn: psycopg.Connection, cloud: Cloud, ip: str, *, quarantine_seconds: float
) -> leases.Lease:
    lease = leases.get(conn, ip)
    if lease.state in ("free", "quarantined"):
        return lease
    if lease.state != "draining":
        lease = leases.begin_drain(conn, ip)
    _delete_resources(cloud, ip, lease)
    return leases.quarantine(conn, ip, seconds=quarantine_seconds)


def _delete_resources(cloud: Cloud, ip: str, lease: leases.Lease) -> None:
    """Delete the VM, then the port. By the recorded ids when there are some (two API calls, no
    listing: listing every tagged resource costs a second or more on a real cloud, per team);
    otherwise, when a crash came between creating a resource and recording it, by the
    deterministic name. A stray the ids do not cover is caught by `verify_released`, whose retry
    comes back here with the ids cleared."""
    if lease.server_id:
        cloud.delete_server(lease.server_id)
    else:
        for server in cloud.list_servers():
            if server.name == server_name(ip):
                cloud.delete_server(server.id)
    if lease.port_id:
        cloud.delete_port(lease.port_id)
    else:
        for port in cloud.list_ports():
            if port.name == port_name(ip):
                cloud.delete_port(port.id)

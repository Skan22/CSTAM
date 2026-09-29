"""IPAM: the lease state machine as short, composable transactions.

Every function runs inside `conn.transaction()`, so it is atomic on its own and becomes a
savepoint when the caller already holds a transaction (the saga claim step composes
`claim_pooled` with the team and job inserts this way). The database trigger is the real
gatekeeper; this module only translates its errors.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

_FIELDS = (
    "state::text, team_id::text, port_id, server_id, expires_at, quarantined_until, row_version"
)
_COLS = f"host(ip), {_FIELDS}"
_COLS_L = f"host(l.ip), {_FIELDS}"  # for UPDATE ... FROM, where the table is aliased
_ILLEGAL = "IPO01"


class IllegalTransition(Exception):
    pass


class LeaseNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Lease:
    ip: str
    state: str
    team_id: str | None
    port_id: str | None
    server_id: str | None
    expires_at: datetime | None
    quarantined_until: datetime | None
    row_version: int


def _lease(row: tuple[Any, ...]) -> Lease:
    return Lease(*row)


def _guarded[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except psycopg.Error as e:
        if e.sqlstate == _ILLEGAL:
            raise IllegalTransition(str(e).splitlines()[0]) from e
        raise


def get(conn: psycopg.Connection, ip: str) -> Lease:
    row = conn.execute(f"SELECT {_COLS} FROM leases WHERE ip = %s", (ip,)).fetchone()
    if row is None:
        raise LeaseNotFound(ip)
    return _lease(row)


def counts(conn: psycopg.Connection) -> dict[str, int]:
    return {s: n for s, n in conn.execute("SELECT state::text, count(*) FROM leases GROUP BY 1")}


def seed(conn: psycopg.Connection, addresses: list[str]) -> int:
    """Create one free lease per pool address. Existing rows are left alone."""
    with conn.transaction():
        cur = conn.execute(
            "INSERT INTO leases (ip) SELECT unnest(%s::inet[]) ON CONFLICT DO NOTHING",
            (addresses,),
        )
        return cur.rowcount


def reserve_free(
    conn: psycopg.Connection, *, team_id: str | None = None, expires_at: datetime | None = None
) -> Lease | None:
    """Take a free address. For the warm pool (no team) it becomes `pooled`; for a team it
    becomes `leased` directly, which is the cold path. None means the pool is exhausted."""
    if (team_id is None) != (expires_at is None):
        raise ValueError("team_id and expires_at go together")
    target = "pooled" if team_id is None else "leased"

    def run() -> Lease | None:
        with conn.transaction():
            row = conn.execute(
                "WITH pick AS (SELECT ip FROM leases WHERE state = 'free'"
                "              ORDER BY ip FOR UPDATE SKIP LOCKED LIMIT 1)"
                " UPDATE leases l SET state = %s::lease_state, team_id = %s, expires_at = %s"
                f" FROM pick WHERE l.ip = pick.ip RETURNING {_COLS_L}",
                (target, team_id, expires_at),
            ).fetchone()
        return _lease(row) if row else None

    return _guarded(run)


def claim_pooled(conn: psycopg.Connection, *, team_id: str, expires_at: datetime) -> Lease | None:
    """Move one pooled lease, with its port and VM, to a team. None when the pool is empty."""

    def run() -> Lease | None:
        with conn.transaction():
            row = conn.execute(
                "WITH pick AS (SELECT ip FROM leases WHERE state = 'pooled'"
                "              ORDER BY ip FOR UPDATE SKIP LOCKED LIMIT 1)"
                " UPDATE leases l SET state = 'leased', team_id = %s, expires_at = %s"
                f" FROM pick WHERE l.ip = pick.ip RETURNING {_COLS_L}",
                (team_id, expires_at),
            ).fetchone()
        return _lease(row) if row else None

    return _guarded(run)


def attach_resources(conn: psycopg.Connection, ip: str, *, port_id: str, server_id: str) -> Lease:
    with conn.transaction():
        row = conn.execute(
            f"UPDATE leases SET port_id = %s, server_id = %s WHERE ip = %s RETURNING {_COLS}",
            (port_id, server_id, ip),
        ).fetchone()
    if row is None:
        raise LeaseNotFound(ip)
    return _lease(row)


def return_to_pool(conn: psycopg.Connection, ip: str) -> Lease:
    """Undo of a claim: a leased VM goes back to the warm pool untouched. Idempotent for a
    lease that is already pooled; any other state is an illegal transition."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE leases SET state = 'pooled', team_id = NULL, expires_at = NULL"
            f" WHERE ip = %s AND state = 'leased' RETURNING {_COLS}", (ip,),
        ).fetchone()
        if row:
            return _lease(row)
        current = get(conn, ip)
    if current.state == "pooled":
        return current
    raise IllegalTransition(f"cannot return {ip} to the pool from {current.state}")


def begin_drain(conn: psycopg.Connection, ip: str) -> Lease:
    def run() -> Lease:
        with conn.transaction():
            row = conn.execute(
                f"UPDATE leases SET state = 'draining' WHERE ip = %s RETURNING {_COLS}", (ip,)
            ).fetchone()
        if row is None:
            raise LeaseNotFound(ip)
        return _lease(row)

    return _guarded(run)


def quarantine(conn: psycopg.Connection, ip: str, *, seconds: float) -> Lease:
    """draining -> quarantined once the VM and port are gone. Idempotent: a second call
    leaves the original quarantine window alone."""

    def run() -> Lease:
        with conn.transaction():
            row = conn.execute(
                "UPDATE leases SET state = 'quarantined', team_id = NULL, port_id = NULL,"
                " server_id = NULL, expires_at = NULL,"
                " quarantined_until = now() + make_interval(secs => %s)"
                f" WHERE ip = %s AND state <> 'quarantined' RETURNING {_COLS}", (seconds, ip),
            ).fetchone()
        return _lease(row) if row else get(conn, ip)

    return _guarded(run)


def release_expired_quarantine(conn: psycopg.Connection) -> list[str]:
    with conn.transaction():
        rows = conn.execute(
            "UPDATE leases SET state = 'free', quarantined_until = NULL"
            " WHERE state = 'quarantined' AND quarantined_until <= now() RETURNING host(ip)"
        ).fetchall()
    return sorted(r[0] for r in rows)


def expired(conn: psycopg.Connection) -> list[Lease]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM leases WHERE state = 'leased' AND expires_at <= now() ORDER BY ip"
    ).fetchall()
    return [_lease(r) for r in rows]


def try_lead(conn: psycopg.Connection, name: str) -> bool:
    """Leader election: a session-level advisory lock. The connection must stay open for as
    long as leadership is held; closing it (or a crash) hands leadership over."""
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s))", (f"ipo.leader.{name}",)
    ).fetchone()
    return bool(row and row[0])


def resign(conn: psycopg.Connection, name: str) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"ipo.leader.{name}",))

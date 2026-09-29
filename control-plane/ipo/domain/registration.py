"""POST /v1/teams, as one short transaction: guard, claim, team, job."""

import re
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

from ipo.domain import leases
from ipo.settings import Settings

SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
KIND = "register_team"


class RegistrationError(Exception):
    pass


class QuotaGuardError(RegistrationError):
    pass


class PoolExhausted(RegistrationError):
    pass


class SlugTaken(RegistrationError):
    pass


class IdempotencyConflict(RegistrationError):
    pass


@dataclass(frozen=True)
class Registration:
    job_id: str
    team_id: str
    ip: str
    path: str  # "warm" or "cold"
    subdomain: str
    created: bool


def _existing(conn: psycopg.Connection, key: str, slug: str) -> Registration | None:
    row = conn.execute(
        "SELECT id::text, team_id::text, payload FROM jobs WHERE idempotency_key = %s", (key,)
    ).fetchone()
    if row is None:
        return None
    job_id, team_id, p = row
    if p["slug"] != slug:
        raise IdempotencyConflict(f"Idempotency-Key {key!r} was used for team {p['slug']!r}")
    return Registration(job_id, team_id, p["ip"], p["path"], p["host"], created=False)


def register_team(
    conn: psycopg.Connection, settings: Settings, *, slug: str, owner: str,
    idempotency_key: str, trace_id: str | None = None,
) -> Registration:
    if not SLUG.match(slug):
        raise ValueError(f"invalid slug {slug!r}: use lowercase letters, digits and hyphens")
    try:
        with conn.transaction():
            return _register(conn, settings, slug, owner, idempotency_key, trace_id)
    except psycopg.errors.UniqueViolation as e:
        # Lost a race with the same key, or the slug is taken. Nothing was committed.
        found = _existing(conn, idempotency_key, slug)
        if found:
            return found
        if e.diag.constraint_name in ("teams_slug_key", "teams_subdomain_key"):
            raise SlugTaken(slug) from e
        raise


def _register(
    conn: psycopg.Connection, settings: Settings, slug: str, owner: str, key: str,
    trace_id: str | None,
) -> Registration:
    found = _existing(conn, key, slug)
    if found:
        return found

    row = conn.execute(
        "SELECT count(*) FROM leases WHERE state IN ('free', 'pooled')"
    ).fetchone()
    assert row
    available: int = row[0]
    if available == 0:
        raise PoolExhausted("no pooled or free addresses remain")
    if available - 1 < settings.reserve_free_ips:
        raise QuotaGuardError(
            f"only {available} addresses are unallocated; registration would drop below the "
            f"reserve of {settings.reserve_free_ips}"
        )

    host = f"{slug}.{settings.domain}"
    row = conn.execute(
        "INSERT INTO teams (slug, subdomain, owner, expires_at)"
        " VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING id::text, expires_at",
        (slug, host, owner, settings.lease_ttl_seconds),
    ).fetchone()
    assert row
    team_id, expires_at = row

    lease = leases.claim_pooled(conn, team_id=team_id, expires_at=expires_at)
    path = "warm"
    if lease is None:
        lease = leases.reserve_free(conn, team_id=team_id, expires_at=expires_at)
        path = "cold"
    if lease is None:
        raise PoolExhausted("no pooled or free addresses remain")

    job = conn.execute(
        "INSERT INTO jobs (kind, team_id, payload, idempotency_key, trace_id)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
        (KIND, team_id, Jsonb({"slug": slug, "owner": owner, "ip": lease.ip, "path": path,
                               "host": host}), key, trace_id),
    ).fetchone()
    assert job
    conn.execute(
        "INSERT INTO saga_steps (job_id, step, status, result, finished_at)"
        " VALUES (%s, 'claim', 'done', %s, now())", (job[0], Jsonb({"ip": lease.ip, "path": path}))
    )
    return Registration(job[0], team_id, lease.ip, path, host, created=True)

import psycopg
import pytest

from ipo.domain import leases
from ipo.domain.registration import (
    IdempotencyConflict,
    PoolExhausted,
    QuotaGuardError,
    SlugTaken,
    register_team,
)
from tests.helpers import make_env


def _rows(conn: psycopg.Connection, sql: str) -> list[tuple[object, ...]]:
    return conn.execute(sql).fetchall()


def test_warm_registration_claims_a_pooled_vm_in_one_transaction(dsn: str) -> None:
    settings, _, connect = make_env(dsn, pooled=2)
    with connect() as c:
        reg = register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
        assert reg.created and reg.path == "warm" and reg.subdomain == "alpha.cstam.felcloud.tn"
        lease = leases.get(c, reg.ip)
        assert lease.state == "leased" and lease.team_id == reg.team_id and lease.server_id
        assert _rows(c, "SELECT state FROM teams") == [("pending",)]
        assert _rows(c, "SELECT state, kind FROM jobs") == [("queued", "register_team")]
        assert _rows(c, "SELECT step, status FROM saga_steps") == [("claim", "done")]


def test_cold_path_reserves_a_free_address_when_the_pool_is_empty(dsn: str) -> None:
    settings, _, connect = make_env(dsn)
    with connect() as c:
        reg = register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
        assert reg.path == "cold"
        lease = leases.get(c, reg.ip)
        assert lease.state == "leased" and lease.server_id is None  # boot happens in the saga


def test_replaying_an_idempotency_key_returns_the_same_job(dsn: str) -> None:
    settings, _, connect = make_env(dsn, pooled=2)
    with connect() as c:
        first = register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
        again = register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
        assert again.job_id == first.job_id and again.team_id == first.team_id
        assert again.created is False
        assert _rows(c, "SELECT count(*) FROM teams") == [(1,)]
        assert leases.counts(c)["leased"] == 1


def test_reusing_a_key_for_a_different_team_is_a_conflict(dsn: str) -> None:
    settings, _, connect = make_env(dsn, pooled=2)
    with connect() as c:
        register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
        with pytest.raises(IdempotencyConflict):
            register_team(c, settings, slug="beta", owner="ada", idempotency_key="k1")


def test_duplicate_slug_is_rejected_and_claims_nothing(dsn: str) -> None:
    settings, _, connect = make_env(dsn, pooled=2)
    with connect() as c:
        register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
        with pytest.raises(SlugTaken):
            register_team(c, settings, slug="alpha", owner="bob", idempotency_key="k2")
        assert leases.counts(c)["leased"] == 1


def test_quota_guard_refuses_below_the_reserve(dsn: str) -> None:
    settings, _, connect = make_env(dsn, addresses=6, reserve_free_ips=5)
    with connect() as c:
        register_team(c, settings, slug="a", owner="o", idempotency_key="1")  # 6 -> 5 left
        with pytest.raises(QuotaGuardError, match="reserve"):
            register_team(c, settings, slug="b", owner="o", idempotency_key="2")
        assert _rows(c, "SELECT count(*) FROM teams") == [(1,)]


def test_exhausted_pool_with_no_free_addresses(dsn: str) -> None:
    settings, _, connect = make_env(dsn, addresses=1)
    with connect() as c:
        register_team(c, settings, slug="a", owner="o", idempotency_key="1")
        with pytest.raises(PoolExhausted):
            register_team(c, settings, slug="b", owner="o", idempotency_key="2")


def test_invalid_slug_is_rejected(dsn: str) -> None:
    settings, _, connect = make_env(dsn, pooled=1)
    with connect() as c, pytest.raises(ValueError, match="slug"):
        register_team(c, settings, slug="Not Valid!", owner="o", idempotency_key="1")

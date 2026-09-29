from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from ipo.domain import leases
from ipo.domain.leases import IllegalTransition

ADDRS = [f"10.20.0.{i}" for i in range(10, 15)]


def make_team(conn: psycopg.Connection, slug: str = "alpha") -> str:
    row = conn.execute(
        "INSERT INTO teams (slug, subdomain, owner, expires_at)"
        " VALUES (%s, %s, 'o', now() + interval '1 hour') RETURNING id",
        (slug, f"{slug}.cstam.felcloud.tn"),
    ).fetchone()
    assert row
    return str(row[0])


def in_one_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def test_seed_is_idempotent(conn: psycopg.Connection) -> None:
    assert leases.seed(conn, ADDRS) == 5
    assert leases.seed(conn, ADDRS + ["10.20.0.15"]) == 1
    assert leases.counts(conn) == {"free": 6}


def test_reserve_free_for_pool_moves_free_to_pooled(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS)
    lease = leases.reserve_free(conn)
    assert lease and lease.state == "pooled" and lease.ip == "10.20.0.10"
    leases.attach_resources(conn, lease.ip, port_id="p1", server_id="s1")
    again = leases.get(conn, lease.ip)
    assert again.port_id == "p1" and again.server_id == "s1"


def test_reserve_free_for_team_is_the_cold_path(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS)
    team = make_team(conn)
    lease = leases.reserve_free(conn, team_id=team, expires_at=in_one_hour())
    assert lease and lease.state == "leased" and lease.team_id == team


def test_reserve_free_returns_none_when_exhausted(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS[:1])
    assert leases.reserve_free(conn)
    assert leases.reserve_free(conn) is None


def test_claim_pooled_takes_a_pooled_lease_only(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS)
    team = make_team(conn)
    assert leases.claim_pooled(conn, team_id=team, expires_at=in_one_hour()) is None
    pooled = leases.reserve_free(conn)
    assert pooled
    claimed = leases.claim_pooled(conn, team_id=team, expires_at=in_one_hour())
    assert claimed and claimed.ip == pooled.ip and claimed.state == "leased"
    assert claimed.team_id == team


def test_return_to_pool_undoes_a_claim(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS)
    team = make_team(conn)
    leases.reserve_free(conn)
    claimed = leases.claim_pooled(conn, team_id=team, expires_at=in_one_hour())
    assert claimed
    back = leases.return_to_pool(conn, claimed.ip)
    assert back.state == "pooled" and back.team_id is None and back.expires_at is None


def test_full_lifecycle_through_quarantine(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS[:1])
    team = make_team(conn)
    lease = leases.reserve_free(conn, team_id=team, expires_at=in_one_hour())
    assert lease
    leases.attach_resources(conn, lease.ip, port_id="p", server_id="s")
    assert leases.begin_drain(conn, lease.ip).state == "draining"
    q = leases.quarantine(conn, lease.ip, seconds=3600)
    assert q.state == "quarantined" and q.port_id is None and q.team_id is None
    assert leases.release_expired_quarantine(conn) == []  # still inside the window
    conn.execute("UPDATE leases SET quarantined_until = now() - interval '1 second'")
    assert leases.release_expired_quarantine(conn) == [lease.ip]
    assert leases.get(conn, lease.ip).state == "free"


def test_illegal_transitions_raise_a_domain_error(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS[:1])
    with pytest.raises(IllegalTransition):
        leases.begin_drain(conn, "10.20.0.10")  # free -> draining
    with pytest.raises(IllegalTransition):
        leases.quarantine(conn, "10.20.0.10", seconds=1)  # free -> quarantined
    with pytest.raises(IllegalTransition):
        leases.return_to_pool(conn, "10.20.0.10")  # free -> pooled via undo


def test_expired_leases_lists_only_overdue_leased(conn: psycopg.Connection) -> None:
    leases.seed(conn, ADDRS)
    a, b = make_team(conn, "a"), make_team(conn, "b")
    la = leases.reserve_free(conn, team_id=a, expires_at=datetime.now(UTC) - timedelta(seconds=5))
    lb = leases.reserve_free(conn, team_id=b, expires_at=in_one_hour())
    assert la and lb
    assert [x.ip for x in leases.expired(conn)] == [la.ip]


def test_leader_lock_admits_one_holder_and_hands_over(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c1, psycopg.connect(dsn, autocommit=True) as c2:
        assert leases.try_lead(c1, "reaper") is True
        assert leases.try_lead(c2, "reaper") is False
        assert leases.try_lead(c2, "reconciler") is True  # independent lock name
        leases.resign(c1, "reaper")
        assert leases.try_lead(c2, "reaper") is True

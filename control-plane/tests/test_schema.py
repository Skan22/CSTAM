"""Exit criteria enforced by the database itself, reached through raw SQL."""

import psycopg
import pytest


def _seed(conn: psycopg.Connection, ip: str = "10.20.0.10") -> None:
    conn.execute("INSERT INTO leases (ip) VALUES (%s)", (ip,))


def _team(conn: psycopg.Connection, slug: str = "alpha") -> str:
    row = conn.execute(
        "INSERT INTO teams (slug, subdomain, owner, expires_at)"
        " VALUES (%s, %s, 'o', now() + interval '1 hour') RETURNING id",
        (slug, f"{slug}.cstam.felcloud.tn"),
    ).fetchone()
    assert row
    return str(row[0])


@pytest.mark.parametrize("target", ["draining", "quarantined"])
def test_free_cannot_skip_ahead(conn: psycopg.Connection, target: str) -> None:
    _seed(conn)
    with pytest.raises(psycopg.Error) as exc:
        conn.execute(f"UPDATE leases SET state = '{target}'")
    assert exc.value.sqlstate == "IPO01"


def test_leased_requires_a_team_and_expiry(conn: psycopg.Connection) -> None:
    """free -> leased is a legal transition, but the row shape must still be valid."""
    _seed(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE leases SET state = 'leased'")


def test_quarantined_cannot_return_to_leased(conn: psycopg.Connection) -> None:
    _seed(conn)
    conn.execute("UPDATE leases SET state = 'pooled'")
    conn.execute("UPDATE leases SET state = 'draining'")
    conn.execute("UPDATE leases SET state = 'quarantined', quarantined_until = now()")
    team = _team(conn)
    with pytest.raises(psycopg.Error) as exc:
        conn.execute(
            "UPDATE leases SET state = 'leased', team_id = %s, expires_at = now(),"
            " quarantined_until = NULL", (team,))
    assert exc.value.sqlstate == "IPO01"


def test_ip_is_immutable_and_rows_cannot_be_deleted(conn: psycopg.Connection) -> None:
    _seed(conn)
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("UPDATE leases SET ip = '10.20.0.99'")
    assert exc.value.sqlstate == "IPO01"
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("DELETE FROM leases")
    assert exc.value.sqlstate == "IPO01"


def test_inserting_a_non_free_lease_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("INSERT INTO leases (ip, state) VALUES ('10.20.0.10', 'pooled')")
    assert exc.value.sqlstate == "IPO01"


def test_row_version_increments_on_every_update(conn: psycopg.Connection) -> None:
    _seed(conn)
    conn.execute("UPDATE leases SET state = 'pooled'")
    conn.execute("UPDATE leases SET port_id = 'p1'")
    row = conn.execute("SELECT row_version FROM leases").fetchone()
    assert row and row[0] == 3


def test_a_team_can_hold_only_one_leased_address(conn: psycopg.Connection) -> None:
    _seed(conn, "10.20.0.10")
    _seed(conn, "10.20.0.11")
    team = _team(conn)
    conn.execute(
        "UPDATE leases SET state = 'leased', team_id = %s, expires_at = now() + interval '1h'"
        " WHERE ip = '10.20.0.10'", (team,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "UPDATE leases SET state = 'leased', team_id = %s, expires_at = now() + interval '1h'"
            " WHERE ip = '10.20.0.11'", (team,))


def test_duplicate_route_host_is_rejected(conn: psycopg.Connection) -> None:
    team = _team(conn)
    conn.execute("INSERT INTO routes (host, team_id, backend_ip, backend_port)"
                 " VALUES ('a.x', %s, '10.20.0.10', 80)", (team,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO routes (host, team_id, backend_ip, backend_port)"
                     " VALUES ('a.x', %s, '10.20.0.11', 80)", (team,))


def test_idempotency_key_is_unique(conn: psycopg.Connection) -> None:
    conn.execute("INSERT INTO jobs (kind, idempotency_key) VALUES ('register', 'k1')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT INTO jobs (kind, idempotency_key) VALUES ('register', 'k1')")


def test_config_versions_strictly_increase_and_are_immutable(conn: psycopg.Connection) -> None:
    for i in range(3):
        conn.execute("INSERT INTO config_versions (version, sha256, signature, body)"
                     " VALUES (999, %s, 's', 'b')", (f"h{i}",))
    versions = [r[0] for r in conn.execute("SELECT version FROM config_versions ORDER BY 1")]
    assert versions == [1, 2, 3]
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("UPDATE config_versions SET body = 'tampered' WHERE version = 2")
    assert exc.value.sqlstate == "IPO02"
    conn.execute("UPDATE config_versions SET status = 'live' WHERE version = 2")
    with pytest.raises(psycopg.Error) as exc:
        conn.execute("DELETE FROM config_versions WHERE version = 1")
    assert exc.value.sqlstate == "IPO02"


def _audit(conn: psycopg.Connection, n: int) -> None:
    for i in range(n):
        conn.execute("INSERT INTO audit_log (actor, action, target, detail)"
                     " VALUES ('admin', 'test', %s, '{\"i\": 1}')", (f"t{i}",))


def test_intact_audit_chain_verifies(conn: psycopg.Connection) -> None:
    _audit(conn, 5)
    row = conn.execute("SELECT verify_audit_chain()").fetchone()
    assert row and row[0] is None


def test_audit_log_rejects_update_delete_truncate(conn: psycopg.Connection) -> None:
    _audit(conn, 2)
    for stmt in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        with pytest.raises(psycopg.Error) as exc:
            conn.execute(stmt)
        assert exc.value.sqlstate == "IPO03"


def test_editing_an_audit_row_is_detected(conn: psycopg.Connection) -> None:
    _audit(conn, 5)
    # A privileged attacker bypasses the trigger; the hash chain must still expose the edit.
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change")
    conn.execute("UPDATE audit_log SET actor = 'mallory' WHERE id = 3")
    conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change")
    row = conn.execute("SELECT verify_audit_chain()").fetchone()
    assert row and row[0] == 3


def test_deleting_an_audit_row_is_detected(conn: psycopg.Connection) -> None:
    _audit(conn, 5)
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change")
    conn.execute("DELETE FROM audit_log WHERE id = 3")
    conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_change")
    row = conn.execute("SELECT verify_audit_chain()").fetchone()
    assert row and row[0] == 4


def test_app_role_cannot_update_audit_log(conn: psycopg.Connection) -> None:
    _audit(conn, 1)
    conn.execute("SET ROLE ipo_app")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE audit_log SET actor = 'x'")
    conn.execute("RESET ROLE")

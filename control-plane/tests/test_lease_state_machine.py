"""Property test: random operation sequences never break the lease state machine.

The model below is deliberately independent of the SQL trigger: it restates the legal
transitions from the plan, and the database must agree with it after every step.
"""

from typing import Any

import psycopg
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from ipo.domain import leases
from ipo.domain.leases import IllegalTransition

LEGAL = {
    ("free", "pooled"), ("free", "leased"), ("pooled", "leased"), ("pooled", "draining"),
    ("leased", "pooled"), ("leased", "draining"), ("draining", "quarantined"),
    ("quarantined", "free"),
}
ADDRS = [f"10.20.0.{i}" for i in range(10, 16)]
STATES = ["free", "pooled", "leased", "draining", "quarantined"]

# Fully-formed column values per target state, so raw updates fail only on the transition rule.
RAW_SET = {
    "free": "state = 'free', team_id = NULL, port_id = NULL, server_id = NULL,"
            " expires_at = NULL, quarantined_until = NULL",
    "pooled": "state = 'pooled', team_id = NULL, expires_at = NULL, quarantined_until = NULL",
    "leased": "state = 'leased', team_id = %(team)s, expires_at = now() + interval '1 hour',"
              " quarantined_until = NULL",
    "draining": "state = 'draining', quarantined_until = NULL",
    "quarantined": "state = 'quarantined', team_id = NULL, port_id = NULL, server_id = NULL,"
                   " expires_at = NULL, quarantined_until = now() + interval '1 hour'",
}


class LeaseMachine(RuleBasedStateMachine):
    dsn = ""

    def __init__(self) -> None:
        super().__init__()
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        self.conn.execute("TRUNCATE leases, teams CASCADE")
        leases.seed(self.conn, ADDRS)
        self.teams = [
            str(self.conn.execute(
                "INSERT INTO teams (slug, subdomain, owner, expires_at)"
                " VALUES (%s, %s, 'o', now() + interval '1 hour') RETURNING id",
                (f"t{i}", f"t{i}.x"),
            ).fetchone()[0])  # type: ignore[index]
            for i in range(3)
        ]
        self.state: dict[str, str] = {ip: "free" for ip in ADDRS}
        self.team_of: dict[str, str | None] = {ip: None for ip in ADDRS}

    def teardown(self) -> None:
        self.conn.close()

    # -- model helpers
    def _ips(self, state: str) -> list[str]:
        return [ip for ip in ADDRS if self.state[ip] == state]

    def _free_teams(self) -> list[str]:
        held = {self.team_of[ip] for ip in self._ips("leased")}
        return [t for t in self.teams if t not in held]

    def _set(self, ip: str, state: str, team: str | None) -> None:
        self.state[ip] = state
        if state in ("free", "pooled", "quarantined"):
            team = None
        self.team_of[ip] = team

    # -- domain operations
    @rule()
    def reserve_for_pool(self) -> None:
        got = leases.reserve_free(self.conn)
        free = self._ips("free")
        if not free:
            assert got is None
        else:
            assert got and got.ip == free[0]
            self._set(got.ip, "pooled", None)

    @precondition(lambda self: bool(self._free_teams()))
    @rule(data=st.data())
    def reserve_for_team(self, data: Any) -> None:
        team = data.draw(st.sampled_from(self._free_teams()))
        got = leases.reserve_free(self.conn, team_id=team, expires_at=_soon(self.conn))
        free = self._ips("free")
        if not free:
            assert got is None
        else:
            assert got and got.ip == free[0]
            self._set(got.ip, "leased", team)

    @precondition(lambda self: bool(self._free_teams()))
    @rule(data=st.data())
    def claim(self, data: Any) -> None:
        team = data.draw(st.sampled_from(self._free_teams()))
        got = leases.claim_pooled(self.conn, team_id=team, expires_at=_soon(self.conn))
        pooled = self._ips("pooled")
        if not pooled:
            assert got is None
        else:
            assert got and got.ip == pooled[0]
            self._set(got.ip, "leased", team)

    @rule(ip=st.sampled_from(ADDRS))
    def return_to_pool(self, ip: str) -> None:
        if self.state[ip] in ("leased", "pooled"):
            assert leases.return_to_pool(self.conn, ip).state == "pooled"
            self._set(ip, "pooled", None)
        else:
            with pytest.raises(IllegalTransition):
                leases.return_to_pool(self.conn, ip)

    @rule(ip=st.sampled_from(ADDRS))
    def drain(self, ip: str) -> None:
        if self.state[ip] in ("leased", "pooled", "draining"):
            assert leases.begin_drain(self.conn, ip).state == "draining"
            self._set(ip, "draining", self.team_of[ip])
        else:
            with pytest.raises(IllegalTransition):
                leases.begin_drain(self.conn, ip)

    @rule(ip=st.sampled_from(ADDRS))
    def quarantine(self, ip: str) -> None:
        if self.state[ip] in ("draining", "quarantined"):
            assert leases.quarantine(self.conn, ip, seconds=3600).state == "quarantined"
            self._set(ip, "quarantined", None)
        else:
            with pytest.raises(IllegalTransition):
                leases.quarantine(self.conn, ip, seconds=3600)

    @rule()
    def release_quarantine(self) -> None:
        self.conn.execute("UPDATE leases SET quarantined_until = now() - interval '1s'"
                          " WHERE state = 'quarantined'")
        released = leases.release_expired_quarantine(self.conn)
        assert released == sorted(self._ips("quarantined"))
        for ip in released:
            self._set(ip, "free", None)

    # -- raw SQL, bypassing the domain layer entirely
    @rule(ip=st.sampled_from(ADDRS), to_state=st.sampled_from(STATES), data=st.data())
    def raw_update(self, ip: str, to_state: str, data: Any) -> None:
        """Any target state at all; most picks are illegal and must be rejected."""
        self._raw(ip, to_state, data.draw(st.sampled_from(self.teams)))

    @rule(ip=st.sampled_from(ADDRS), data=st.data())
    def raw_legal_step(self, ip: str, data: Any) -> None:
        """A step the model says is legal, so sequences reach deep states such as quarantine."""
        options = [t for (f, t) in sorted(LEGAL) if f == self.state[ip]]
        self._raw(ip, data.draw(st.sampled_from(options)), data.draw(st.sampled_from(self.teams)))

    def _raw(self, ip: str, to_state: str, team: str) -> None:
        current = self.state[ip]
        if to_state == current:
            return
        legal = (current, to_state) in LEGAL
        if legal and to_state == "leased" and team not in self._free_teams():
            return  # would trip the one-lease-per-team index, which is tested elsewhere
        sql = f"UPDATE leases SET {RAW_SET[to_state]} WHERE ip = %(ip)s"
        if not legal:
            with pytest.raises(psycopg.Error) as exc:
                self.conn.execute(sql, {"ip": ip, "team": team})
            assert exc.value.sqlstate == "IPO01"
            return
        self.conn.execute(sql, {"ip": ip, "team": team})
        self._set(ip, to_state, team if to_state == "leased" else self.team_of[ip])

    # -- invariants
    @invariant()
    def database_agrees_with_the_model(self) -> None:
        rows = {
            r[0]: (r[1], r[2])
            for r in self.conn.execute("SELECT host(ip), state::text, team_id::text FROM leases")
        }
        assert set(rows) == set(ADDRS)
        for ip in ADDRS:
            state, team = rows[ip]
            assert state == self.state[ip], ip
            if state in ("leased", "draining") and self.team_of[ip]:
                assert team == self.team_of[ip], ip

    @invariant()
    def no_team_holds_two_leases(self) -> None:
        dup = self.conn.execute(
            "SELECT team_id FROM leases WHERE state = 'leased' GROUP BY team_id HAVING count(*) > 1"
        ).fetchall()
        assert dup == []


def _soon(conn: psycopg.Connection) -> Any:
    row = conn.execute("SELECT now() + interval '1 hour'").fetchone()
    assert row
    return row[0]


def test_random_operation_sequences_never_break_the_state_machine(dsn: str) -> None:
    LeaseMachine.dsn = dsn
    run_state_machine_as_test(  # type: ignore[no-untyped-call]
        LeaseMachine,
        settings=settings(max_examples=60, stateful_step_count=40, deadline=None),
    )

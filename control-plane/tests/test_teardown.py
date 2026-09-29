from collections.abc import Callable

import psycopg
import pytest

from ipo.adapters.openstack.fake import FakeCloud
from ipo.controllers.pool import PoolManager
from ipo.controllers.reaper import Reaper
from ipo.controllers.register_saga import Deps
from ipo.controllers.saga import SagaRunner, WorkerCrash
from ipo.controllers.workers import build_runner
from ipo.domain import leases
from ipo.domain.registration import register_team
from ipo.domain.teardown import TeamBusy, enqueue_teardown
from ipo.settings import Settings
from tests.helpers import FakeGateways, FakeProber, make_env

Env = tuple[Settings, FakeCloud, Callable[[], psycopg.Connection]]


def runner(env: Env, gw: FakeGateways, **kw: object) -> SagaRunner:
    settings, cloud, connect = env
    return build_runner(connect, Deps(cloud, gw, FakeProber(), settings), worker_id="w",
                        sleep=lambda s: None, **kw)  # type: ignore[arg-type]


def register_active(env: Env, gw: FakeGateways, slug: str = "alpha") -> tuple[str, str, str]:
    """Register a team and run its saga; returns (team_id, ip, host)."""
    settings, _, connect = env
    with connect() as c:
        reg = register_team(c, settings, slug=slug, owner="ada", idempotency_key=f"k-{slug}")
    assert runner(env, gw).run_once() == "succeeded"
    return reg.team_id, reg.ip, reg.subdomain


def state(env: Env) -> dict[str, object]:
    _, cloud, connect = env
    with connect() as c:
        return {
            "teams": c.execute("SELECT state, count(*) FROM teams GROUP BY 1").fetchall(),
            "routes": c.execute("SELECT count(*) FROM routes").fetchone(),
            "leases": leases.counts(c),
            "servers": len(cloud.servers),
            "ports": len(cloud.ports),
        }


def test_teardown_removes_route_first_then_the_vm_then_quarantines(dsn: str) -> None:
    env = make_env(dsn, quarantine_seconds=60)
    gw = FakeGateways()
    team_id, ip, host = register_active(env, gw)
    _, cloud, connect = env
    seen: list[tuple[object, int]] = []
    gw.on_removal = lambda c: seen.append(
        (c.execute("SELECT count(*) FROM routes").fetchone(), len(cloud.servers)))
    with connect() as c:
        job_id = enqueue_teardown(c, team_id, reason="api", actor="ada")
        assert c.execute("SELECT state FROM teams").fetchone() == ("draining",)
    assert job_id
    assert runner(env, gw).run_once() == "succeeded"
    assert seen == [((0,), 1)]  # route already gone, VM still there when the ack is awaited
    assert gw.removed == [host]
    with connect() as c:
        assert c.execute("SELECT state FROM teams").fetchone() == ("deleted",)
        lease = leases.get(c, ip)
        assert lease.state == "quarantined" and lease.quarantined_until is not None
        actions = [a for (a,) in c.execute("SELECT action FROM audit_log ORDER BY id")]
        assert "team.deleted" in actions
        assert c.execute("SELECT verify_audit_chain()").fetchone() == (None,)
    assert state(env)["servers"] == 0 and state(env)["ports"] == 0


def test_enqueue_is_idempotent_and_rejects_bad_targets(dsn: str) -> None:
    env = make_env(dsn)
    gw = FakeGateways()
    team_id, _, _ = register_active(env, gw)
    _, _, connect = env
    with connect() as c:
        first = enqueue_teardown(c, team_id, reason="api", actor="ada")
        assert enqueue_teardown(c, team_id, reason="api", actor="ada") == first
        with pytest.raises(LookupError):
            enqueue_teardown(c, "00000000-0000-0000-0000-000000000000", reason="x", actor="x")
    assert runner(env, gw).run_once() == "succeeded"
    with connect() as c:
        assert enqueue_teardown(c, team_id, reason="api", actor="ada") is None  # already deleted


def test_a_team_still_registering_cannot_be_torn_down(dsn: str) -> None:
    settings, _, connect = env = make_env(dsn)
    with connect() as c:
        reg = register_team(c, settings, slug="beta", owner="o", idempotency_key="k")
        with pytest.raises(TeamBusy):
            enqueue_teardown(c, reg.team_id, reason="api", actor="o")
    del env


def test_gateways_that_never_ack_the_removal_do_not_block_teardown(dsn: str) -> None:
    env = make_env(dsn)
    gw = FakeGateways()
    team_id, ip, _ = register_active(env, gw)
    gw.fail_removal = TimeoutError("gw-b is down")
    _, _, connect = env
    with connect() as c:
        enqueue_teardown(c, team_id, reason="api", actor="ada")
    assert runner(env, gw).run_once() == "succeeded"
    assert state(env)["servers"] == 0
    with connect() as c:
        assert leases.get(c, ip).state == "quarantined"


# ---------------------------------------------------------------- reaper
def _expire(connect: Callable[[], psycopg.Connection], ip: str) -> None:
    with connect() as c:
        c.execute("UPDATE leases SET expires_at = now() - interval '1 second' WHERE ip = %s",
                  (ip,))


def test_reaper_tears_down_only_expired_leases_and_only_once(dsn: str) -> None:
    env = make_env(dsn)
    gw = FakeGateways()
    _, connect = env[1], env[2]
    _, ip_a, _ = register_active(env, gw, "aa")
    _, ip_b, _ = register_active(env, gw, "bb")
    _expire(connect, ip_a)
    reaper = Reaper(connect)
    assert reaper.run_once().torn_down == 1
    assert reaper.run_once().torn_down == 0  # a teardown job already exists
    with connect() as c:
        rows = c.execute("SELECT slug, state FROM teams ORDER BY slug").fetchall()
    assert rows == [("aa", "draining"), ("bb", "active")]
    assert runner(env, gw).run_once() == "succeeded"
    with connect() as c:
        assert leases.get(c, ip_b).state == "leased"


def test_reaper_frees_quarantined_addresses_once_the_window_passes(dsn: str) -> None:
    env = make_env(dsn, quarantine_seconds=0)
    gw = FakeGateways()
    team_id, ip, _ = register_active(env, gw)
    _, _, connect = env
    with connect() as c:
        enqueue_teardown(c, team_id, reason="api", actor="ada")
    runner(env, gw).run_once()
    assert Reaper(connect).run_once().freed == [ip]
    with connect() as c:
        assert leases.get(c, ip).state == "free"


# ----------------------------------------------------------- exit criteria
def test_100_create_and_delete_cycles_leave_nothing_behind(dsn: str) -> None:
    settings, cloud, connect = env = make_env(dsn, quarantine_seconds=0)
    gw = FakeGateways()
    pool = PoolManager(cloud, settings, connect)
    reaper = Reaper(connect)
    pool.reconcile(target=2)
    for i in range(100):
        team_id, _, _ = register_active(env, gw, f"team-{i}")
        with connect() as c:
            enqueue_teardown(c, team_id, reason="api", actor="loop")
        assert runner(env, gw).run_once() == "succeeded"
        reaper.run_once()
        pool.reconcile(target=2)
    with connect() as c:
        counts = leases.counts(c)
        assert c.execute("SELECT count(*) FROM routes").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM teams WHERE state <> 'deleted'").fetchone() == (0,)
        assert counts.get("leased", 0) == counts.get("draining", 0) == 0
        assert counts.get("quarantined", 0) == 0
    pooled = counts["pooled"]
    assert len(cloud.servers) == len(cloud.ports) == pooled == 2  # only the warm pool remains


TEARDOWN_BOUNDARIES = [f"{w}:{s}" for s in ("drain", "await_route_removal", "release", "finalize")
                       for w in ("before", "after")]


@pytest.mark.parametrize("at", TEARDOWN_BOUNDARIES)
def test_worker_killed_mid_teardown_is_finished_by_the_reconciler(dsn: str, at: str) -> None:
    from ipo.controllers.reconciler import Reconciler

    env = make_env(dsn)
    settings, cloud, connect = env
    gw = FakeGateways()
    team_id, ip, host = register_active(env, gw)
    with connect() as c:
        enqueue_teardown(c, team_id, reason="api", actor="ada")
    when, step = at.split(":")

    def crash(event: str, name: str) -> None:
        if (event, name) == (when, step):
            raise WorkerCrash(at)

    with pytest.raises(WorkerCrash):
        runner(env, gw, hook=crash).run_once()
    Reconciler(cloud, settings, connect, grace_seconds=0).run_once()
    with connect() as c:
        assert c.execute("SELECT state FROM teams").fetchone() == ("deleted",)
        assert leases.get(c, ip).state in ("quarantined", "free")
        assert c.execute("SELECT count(*) FROM routes").fetchone() == (0,)
    assert not cloud.servers and not cloud.ports
    # a late-resuming worker finds nothing left to do and changes nothing
    runner(env, gw).run_once()
    assert state(env)["servers"] == 0
    del host

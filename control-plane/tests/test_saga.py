from collections.abc import Callable

import psycopg
import pytest

from ipo.adapters.openstack.base import CloudError
from ipo.adapters.openstack.fake import FakeCloud
from ipo.controllers.register_saga import STEP_NAMES, Deps, build_runner
from ipo.controllers.saga import SagaRunner, WorkerCrash
from ipo.domain import leases
from ipo.domain.registration import Registration, register_team
from ipo.settings import Settings
from tests.helpers import FakeGateways, FakeProber, make_env

Env = tuple[Settings, FakeCloud, Callable[[], psycopg.Connection]]
RUNNABLE = [s for s in STEP_NAMES if s != "claim"]


def setup(dsn: str, *, warm: bool) -> tuple[Env, Registration, FakeGateways, FakeProber]:
    settings, cloud, connect = make_env(dsn, pooled=2 if warm else 0)
    with connect() as c:
        reg = register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k1")
    return (settings, cloud, connect), reg, FakeGateways(), FakeProber()


def runner(env: Env, gw: FakeGateways, pr: FakeProber, **kw: object) -> SagaRunner:
    settings, cloud, connect = env
    return build_runner(connect, Deps(cloud, gw, pr, settings), worker_id="w", sleep=lambda s: None,
                        **kw)  # type: ignore[arg-type]


def snapshot(env: Env) -> dict[str, object]:
    _, cloud, connect = env
    with connect() as c:
        return {
            "team": c.execute("SELECT state FROM teams").fetchone(),
            "job": c.execute("SELECT state FROM jobs").fetchone(),
            "routes": c.execute("SELECT count(*) FROM routes").fetchone(),
            "leases": leases.counts(c),
            "servers": len(cloud.servers),
            "ports": len(cloud.ports),
            "team_tags": sum(1 for s in cloud.servers.values() if "ipo.team" in s.metadata),
        }


@pytest.mark.parametrize("warm", [True, False], ids=["warm", "cold"])
def test_happy_path_ends_with_a_live_team(dsn: str, warm: bool) -> None:
    env, reg, gw, pr = setup(dsn, warm=warm)
    assert runner(env, gw, pr).run_once() == "succeeded"
    snap = snapshot(env)
    assert snap["team"] == ("active",) and snap["job"] == ("succeeded",)
    assert snap["routes"] == (1,) and snap["team_tags"] == 1
    assert gw.waited == [reg.subdomain] and pr.probed == [reg.subdomain]
    _, _, connect = env
    with connect() as c:
        steps = c.execute("SELECT step, status FROM saga_steps").fetchall()
    assert {s for s, _ in steps} == set(STEP_NAMES) and {st for _, st in steps} == {"done"}
    lease = leases.get(connect(), reg.ip)
    assert lease.state == "leased" and lease.server_id and lease.port_id


def test_nothing_to_do_returns_none(dsn: str) -> None:
    env, _, gw, pr = setup(dsn, warm=True)
    r = runner(env, gw, pr)
    assert r.run_once() == "succeeded"
    assert r.run_once() is None


def test_transient_failure_is_retried(dsn: str) -> None:
    env, _, gw, pr = setup(dsn, warm=False)
    env[1].inject("ensure_server", times=2)
    assert runner(env, gw, pr).run_once() == "succeeded"
    assert env[1].calls["ensure_server"] == 3


def _break(step: str, env: Env, gw: FakeGateways, pr: FakeProber) -> None:
    _, cloud, _ = env
    err = CloudError(f"boom in {step}")
    if step == "provision":
        cloud.inject("ensure_server", err, times=99)
    elif step == "personalize":
        cloud.inject("set_server_metadata", err, times=99)
    elif step == "route":
        env[2]().execute("ALTER TABLE routes ADD CONSTRAINT nope CHECK (false) NOT VALID")
    elif step == "converge":
        gw.fail = err
    elif step == "probe":
        pr.fail = err
    elif step == "activate":
        env[2]().execute(
            "ALTER TABLE teams ADD CONSTRAINT nope CHECK (state <> 'active') NOT VALID")


@pytest.mark.parametrize("step", RUNNABLE)
@pytest.mark.parametrize("warm", [True, False], ids=["warm", "cold"])
def test_permanent_failure_at_any_step_undoes_everything(dsn: str, warm: bool, step: str) -> None:
    if step == "provision" and warm:
        pytest.skip("a warm claim has nothing to provision")
    env, reg, gw, pr = setup(dsn, warm=warm)
    _break(step, env, gw, pr)
    assert runner(env, gw, pr, max_step_attempts=2).run_once() == "compensated"
    snap = snapshot(env)
    assert snap["team"] == ("failed",) and snap["job"] == ("compensated",)
    assert snap["routes"] == (0,) and snap["team_tags"] == 0
    counts = snap["leases"]
    assert isinstance(counts, dict) and counts.get("leased", 0) == 0
    if warm:
        assert counts["pooled"] == 2 and snap["servers"] == 2  # the VM went back to the pool
    else:
        assert snap["servers"] == 0 and snap["ports"] == 0  # cold resources fully deleted
        assert counts.get("pooled", 0) == 0 and counts.get("draining", 0) == 0
        with env[2]() as c:
            assert leases.get(c, reg.ip).state in ("quarantined", "free")


BOUNDARIES = [f"{when}:{s}" for s in RUNNABLE for when in ("before", "after")]


class CrashAt:
    def __init__(self, at: str) -> None:
        self.at = at

    def __call__(self, event: str, step: str) -> None:
        if event == self.at.split(":")[0] and step == self.at.split(":")[1]:
            raise WorkerCrash(self.at)


@pytest.mark.parametrize("at", BOUNDARIES)
@pytest.mark.parametrize("warm", [True, False], ids=["warm", "cold"])
def test_worker_killed_at_any_boundary_resumes_to_a_live_team(
    dsn: str, warm: bool, at: str
) -> None:
    env, reg, gw, pr = setup(dsn, warm=warm)
    with pytest.raises(WorkerCrash):
        runner(env, gw, pr, hook=CrashAt(at)).run_once()
    assert snapshot(env)["job"] == ("running",)  # the dead worker's lock is still held
    assert runner(env, gw, pr, stale_seconds=0).run_once() == "succeeded"
    snap = snapshot(env)
    assert snap["team"] == ("active",) and snap["routes"] == (1,) and snap["team_tags"] == 1
    assert snap["servers"] == snap["ports"] == (2 if warm else 1)
    assert snap["leases"] == ({"leased": 1, "pooled": 1, "free": 18} if warm
                              else {"leased": 1, "free": 19})
    with env[2]() as c:
        assert leases.get(c, reg.ip).server_id


@pytest.mark.parametrize("at", BOUNDARIES)
@pytest.mark.parametrize("warm", [True, False], ids=["warm", "cold"])
def test_worker_killed_then_the_retry_fails_ends_in_a_clean_undo(
    dsn: str, warm: bool, at: str
) -> None:
    """Whatever the crash point, the job ends live or fully undone, never half-built."""
    env, _, gw, pr = setup(dsn, warm=warm)
    with pytest.raises(WorkerCrash):
        runner(env, gw, pr, hook=CrashAt(at)).run_once()
    gw.fail = CloudError("gateway unreachable")  # converge can no longer succeed
    result = runner(env, gw, pr, stale_seconds=0, max_step_attempts=1).run_once()

    order = [f"{when}:{s}" for s in RUNNABLE for when in ("before", "after")]
    converge_finished = order.index(at) >= order.index("after:converge")
    if converge_finished:
        assert result == "succeeded"  # the broken gateway is never consulted again
        assert snapshot(env)["team"] == ("active",)
        return
    assert result == "compensated"
    snap = snapshot(env)
    assert snap["team"] == ("failed",) and snap["routes"] == (0,) and snap["team_tags"] == 0
    counts = snap["leases"]
    assert isinstance(counts, dict) and counts.get("leased", 0) == 0
    assert snap["servers"] == snap["ports"] == (2 if warm else 0)


def test_a_poison_job_is_compensated_instead_of_crashing_workers_forever(dsn: str) -> None:
    env, _, gw, pr = setup(dsn, warm=True)
    for _ in range(3):
        with pytest.raises(WorkerCrash):
            runner(env, gw, pr, hook=CrashAt("before:route"),
                   stale_seconds=0).run_once()
    result = runner(env, gw, pr, stale_seconds=0, max_job_attempts=3).run_once()
    assert result == "compensated"
    assert snapshot(env)["team"] == ("failed",)


def test_two_workers_never_run_the_same_job(dsn: str) -> None:
    env, _, gw, pr = setup(dsn, warm=True)
    a, b = runner(env, gw, pr), runner(env, gw, pr)
    with env[2]() as c1, env[2]() as c2:
        job = a.claim(c1)
        assert job is not None
        assert b.claim(c2) is None  # locked and not stale


def _sample(name: str, **labels: str) -> float:
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_saga_metrics_count_steps_and_outcomes(dsn: str) -> None:
    env, _, gw, pr = setup(dsn, warm=True)
    steps = _sample("ipo_saga_step_duration_seconds_count", step="route")
    done = _sample("ipo_saga_jobs_total", kind="register_team", outcome="succeeded")
    assert runner(env, gw, pr).run_once() == "succeeded"
    assert _sample("ipo_saga_step_duration_seconds_count", step="route") == steps + 1
    assert _sample("ipo_saga_jobs_total", kind="register_team", outcome="succeeded") == done + 1


def test_saga_metrics_count_compensation(dsn: str) -> None:
    env, _, gw, pr = setup(dsn, warm=True)
    before = _sample("ipo_saga_jobs_total", kind="register_team", outcome="compensated")
    _break("probe", env, gw, pr)
    assert runner(env, gw, pr).run_once() == "compensated"
    assert _sample("ipo_saga_jobs_total", kind="register_team", outcome="compensated") == before + 1

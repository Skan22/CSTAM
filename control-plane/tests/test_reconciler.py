import pytest

from ipo import metrics
from ipo.adapters.openstack.base import port_name, server_name
from ipo.controllers.reconciler import Reconciler
from ipo.domain import leases
from ipo.domain.registration import register_team
from tests.helpers import FakeGateways, make_env
from tests.test_teardown import Env, register_active


def rec(env: Env, **kw: object) -> Reconciler:
    settings, cloud, connect = env
    return Reconciler(cloud, settings, connect, grace_seconds=0, **kw)  # type: ignore[arg-type]


def repairs(env: Env, **kw: object) -> dict[str, int]:
    return {k: v for k, v in rec(env, **kw).run_once().items() if v}


def test_a_healthy_platform_needs_no_repairs(dsn: str) -> None:
    env = make_env(dsn, pooled=2)
    register_active(env, FakeGateways())
    assert repairs(env) == {}


def test_a_tagged_port_without_a_lease_is_deleted(dsn: str) -> None:
    env = make_env(dsn)
    _, cloud, _ = env
    cloud.ensure_port(name=port_name("10.20.0.15"), ip="10.20.0.15")  # lease is 'free'
    cloud.ensure_port(name=port_name("10.20.0.99"), ip="10.20.0.99")  # no lease at all
    assert repairs(env) == {"orphan_port": 2}
    assert not cloud.ports
    assert repairs(env) == {}  # converged


def test_a_tagged_vm_with_no_team_and_not_pooled_is_deleted(dsn: str) -> None:
    env = make_env(dsn, pooled=1)
    _, cloud, _ = env
    port = cloud.ensure_port(name=port_name("10.20.0.20"), ip="10.20.0.20")
    cloud.ensure_server(name=server_name("10.20.0.20"), port_id=port.id)
    assert repairs(env) == {"orphan_vm": 1, "orphan_port": 1}
    assert len(cloud.servers) == 1  # the pooled VM survives


def test_resources_younger_than_the_grace_period_are_left_alone(dsn: str) -> None:
    env = make_env(dsn)
    settings, cloud, connect = env
    cloud.ensure_port(name=port_name("10.20.0.15"), ip="10.20.0.15")
    young = Reconciler(cloud, settings, connect, grace_seconds=30)
    assert not any(young.run_once().values())
    later = Reconciler(cloud, settings, connect, grace_seconds=30,
                       now=lambda: __import__("time").time() + 60)
    assert later.run_once()["orphan_port"] == 1


def test_a_leased_lease_whose_vm_vanished_fails_the_team_and_tears_it_down(dsn: str) -> None:
    env = make_env(dsn)
    _, cloud, connect = env
    team_id, ip, _ = register_active(env, FakeGateways())
    cloud.servers.clear()
    assert repairs(env)["vm_gone"] == 1
    with connect() as c:
        actions = [a for (a,) in c.execute("SELECT action FROM audit_log ORDER BY id")]
        assert actions[-3:] == ["team.vm_gone", "team.teardown_started", "team.deleted"]
        assert c.execute("SELECT state FROM teams").fetchone() == ("deleted",)
        assert leases.get(c, ip).state in ("quarantined", "free")
        assert c.execute("SELECT count(*) FROM routes").fetchone() == (0,)
    assert not cloud.ports  # the orphaned port went with it
    del team_id


def test_nothing_is_repaired_while_a_lease_is_younger_than_the_grace_period(dsn: str) -> None:
    env = make_env(dsn)
    settings, cloud, connect = env
    register_active(env, FakeGateways())
    cloud.servers.clear()
    patient = Reconciler(cloud, settings, connect, grace_seconds=3600)
    assert not any(patient.run_once().values())


def test_routes_of_inactive_teams_are_removed_but_a_registering_team_keeps_its_route(
    dsn: str,
) -> None:
    env = make_env(dsn)
    settings, _, connect = env
    with connect() as c:
        reg = register_team(c, settings, slug="mid", owner="o", idempotency_key="k")  # pending
        c.execute("INSERT INTO routes (host, team_id, backend_ip, backend_port)"
                  " VALUES (%s, %s, %s, 80)", (reg.subdomain, reg.team_id, reg.ip))
    assert repairs(env) == {}  # its registration job is still queued
    with connect() as c:
        c.execute("UPDATE teams SET state = 'deleted'")
    assert repairs(env) == {"stale_route": 1}
    with connect() as c:
        assert c.execute("SELECT count(*) FROM routes").fetchone() == (0,)


def test_a_lease_stuck_in_draining_is_driven_to_quarantine(dsn: str) -> None:
    env = make_env(dsn, pooled=1, quarantine_seconds=60)
    _, cloud, connect = env
    with connect() as c:
        pooled = c.execute("SELECT host(ip) FROM leases WHERE state = 'pooled'").fetchone()
        assert pooled
        leases.begin_drain(c, pooled[0])
    assert repairs(env)["stuck_lease"] == 1
    with connect() as c:
        assert leases.get(c, pooled[0]).state == "quarantined"
    assert not cloud.servers and not cloud.ports


def test_a_lease_stuck_past_its_quarantine_deadline_is_freed(dsn: str) -> None:
    env = make_env(dsn, pooled=1)
    _, _, connect = env
    with connect() as c:
        (ip,) = c.execute("SELECT host(ip) FROM leases WHERE state = 'pooled'").fetchone()  # type: ignore[misc]
    settings, cloud, _ = env
    from ipo.domain.teardown import release_resources

    with connect() as c:
        release_resources(c, cloud, ip, quarantine_seconds=0)
    assert repairs(env) == {"stuck_lease": 1}
    with connect() as c:
        assert leases.get(c, ip).state == "free"


def test_a_gateway_behind_the_desired_version_gets_a_repush(dsn: str) -> None:
    env = make_env(dsn)
    _, _, connect = env
    pushed: list[int] = []
    with connect() as c:
        for i in (1, 2):
            c.execute("INSERT INTO config_versions (version, sha256, signature, body)"
                      " VALUES (0, %s, 's', %s)", (f"h{i}", f'{{"v":{i}}}'))
        c.execute("INSERT INTO gateway_status (gateway, live_version) VALUES ('gw-a', 2),"
                  " ('gw-b', 1)")
    assert repairs(env, repush=lambda v: pushed.append(v.version)) == {"gateway_behind": 1}
    assert pushed == [2]
    with connect() as c:
        c.execute("UPDATE gateway_status SET live_version = 2")
    pushed.clear()
    assert repairs(env, repush=lambda v: pushed.append(v.version)) == {}
    assert pushed == []


def test_each_repair_is_counted_in_the_metric(dsn: str) -> None:
    env = make_env(dsn)
    _, cloud, _ = env
    before = metrics.RECONCILER_REPAIRS.labels("orphan_port")._value.get()
    cloud.ensure_port(name=port_name("10.20.0.15"), ip="10.20.0.15")
    rec(env).run_once()
    after = metrics.RECONCILER_REPAIRS.labels("orphan_port")._value.get()
    assert after == before + 1


def test_a_failing_cloud_call_aborts_the_cycle_and_the_next_one_repairs(dsn: str) -> None:
    env = make_env(dsn)
    _, cloud, _ = env
    cloud.ensure_port(name=port_name("10.20.0.15"), ip="10.20.0.15")
    cloud.inject("list_servers")
    with pytest.raises(Exception, match="injected"):
        rec(env).run_once()
    assert repairs(env) == {"orphan_port": 1}  # the next cycle repairs it

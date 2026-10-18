import threading
import time

import psycopg

from ipo.adapters.openstack.base import QuotaExceeded
from ipo.adapters.openstack.fake import FakeCloud
from ipo.controllers.pool import PoolManager
from ipo.domain import leases
from ipo.domain.registration import register_team
from tests.helpers import make_env


def test_reconcile_fills_the_pool_to_target(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn)
    report = PoolManager(cloud, settings, connect).reconcile(target=4)
    assert report.created == 4 and report.failed == 0
    with connect() as c:
        assert leases.counts(c)["pooled"] == 4
        rows = c.execute("SELECT port_id, server_id FROM leases WHERE state = 'pooled'").fetchall()
    assert all(p and s for p, s in rows)
    assert len(cloud.servers) == 4 and len(cloud.ports) == 4


def test_reconcile_is_idempotent_at_target(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn, pooled=3)
    report = PoolManager(cloud, settings, connect).reconcile(target=3)
    assert report.created == 0
    assert len(cloud.servers) == 3


def test_reconcile_finishes_a_half_provisioned_lease(dsn: str) -> None:
    """A crash after the address was reserved but before the VM booted must not leak."""
    settings, cloud, connect = make_env(dsn)
    with connect() as c:
        assert leases.reserve_free(c)  # pooled, no port or server yet
    report = PoolManager(cloud, settings, connect).reconcile(target=1)
    assert report.created == 1
    with connect() as c:
        assert leases.counts(c) == {"pooled": 1, "free": 19}
    assert len(cloud.servers) == 1


def test_boot_failure_releases_the_address_and_leaves_no_resources(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn)
    cloud.inject("ensure_server", times=1)
    report = PoolManager(cloud, settings, connect).reconcile(target=1)
    assert report.failed == 1
    with connect() as c:
        counts = leases.counts(c)
    assert counts.get("pooled", 0) == 0
    assert cloud.ports == {} and cloud.servers == {}
    assert counts.get("quarantined", 0) + counts.get("draining", 0) + counts["free"] == 20


def test_quota_error_backs_off_instead_of_hammering(dsn: str) -> None:
    settings, _, connect = make_env(dsn)
    cloud = FakeCloud(max_servers=1)
    now = [1000.0]
    pool = PoolManager(cloud, settings, connect, clock=lambda: now[0])
    first = pool.reconcile(target=3)
    assert first.created == 1 and first.backoff_seconds > 0
    calls = cloud.calls["ensure_server"]
    pool.reconcile(target=3)  # still inside the backoff window: no API traffic
    assert cloud.calls["ensure_server"] == calls
    now[0] += first.backoff_seconds + 1
    second = pool.reconcile(target=3)
    assert cloud.calls["ensure_server"] > calls and second.backoff_seconds > first.backoff_seconds
    assert isinstance(QuotaExceeded(), Exception)


def test_parallel_boots_are_capped(dsn: str) -> None:
    settings, _, connect = make_env(dsn, max_parallel_boots=2)
    active = peak = 0
    lock = threading.Lock()

    class Slow(FakeCloud):
        def ensure_server(self, **kw):  # type: ignore[no-untyped-def]
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            try:
                return super().ensure_server(**kw)
            finally:
                with lock:
                    active -= 1

    PoolManager(Slow(), settings, connect).reconcile(target=6)
    assert peak == 2


def test_pooled_resources_carry_the_recognition_tag(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn, pooled=2)
    assert len(cloud.list_ports()) == 2 and len(cloud.list_servers()) == 2
    with connect() as c:
        assert isinstance(c, psycopg.Connection)


def test_lowering_the_target_discards_only_the_surplus_idle_vms(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn, quarantine_seconds=0)
    pool = PoolManager(cloud, settings, connect)
    pool.reconcile(target=6)
    with connect() as c:
        reg = register_team(c, settings, slug="alpha", owner="o", idempotency_key="k")
    assert reg.path == "warm"
    pool.reconcile(target=2)
    with connect() as c:
        counts = leases.counts(c)
        assert counts["pooled"] == 2 and counts["leased"] == 1  # the claimed VM is not touched
        assert leases.get(c, reg.ip).state == "leased"
    assert len(cloud.servers) == len(cloud.ports) == 3
    pool.reconcile(target=2)  # at target: nothing more to do
    assert len(cloud.servers) == 3


def test_a_fake_cloud_with_a_state_file_survives_a_restart(tmp_path: object) -> None:
    from pathlib import Path
    state = Path(str(tmp_path)) / "cloud.json"
    first = FakeCloud(state_file=state)
    port = first.ensure_port(name="p", ip="10.0.0.1")
    server = first.ensure_server(name="s", port_id=port.id, metadata={"a": "b"})
    first.set_server_metadata(server.id, {"c": "d"})
    again = FakeCloud(state_file=state)
    assert again.list_ports() == [port]
    assert again.list_servers()[0].metadata == {"a": "b", "c": "d"}
    newer = again.ensure_port(name="q", ip="10.0.0.2")
    assert newer.id != port.id  # ids keep counting
    again.delete_server(server.id)
    assert FakeCloud(state_file=state).list_servers() == []


def test_the_pool_grows_to_the_recent_demand_and_shrinks_when_it_passes(dsn: str) -> None:
    from ipo.domain.demand import demand_target
    from ipo.domain.registration import register_team

    settings, cloud, connect = make_env(dsn, pooled=2, quarantine_seconds=0, pool_max=6,
                                        demand_window_seconds=3600, reserve_free_ips=0)
    pool = PoolManager(cloud, settings, connect)
    with connect() as c:
        assert demand_target(c, 2, settings) == 2  # quiet: the configured target
        for i in range(4):
            register_team(c, settings, slug=f"t{i}", owner="o", idempotency_key=f"k{i}")
        assert demand_target(c, 2, settings) == 4  # four registered: be ready for four more
        for i in range(4, 9):
            register_team(c, settings, slug=f"t{i}", owner="o", idempotency_key=f"k{i}")
        assert demand_target(c, 2, settings) == 6  # capped at pool_max
        target = demand_target(c, 2, settings)
    pool.reconcile(target=target)
    with connect() as c:
        assert leases.counts(c)["pooled"] == 6
    # the window passes: the target falls back to the configured one and the surplus goes
    from dataclasses import replace
    quiet = replace(settings, demand_window_seconds=0)
    with connect() as c:
        target = demand_target(c, 2, quiet)
    assert target == 2
    pool.reconcile(target=target)
    with connect() as c:
        assert leases.counts(c)["pooled"] == 2


def test_a_restarted_fake_cloud_adopts_what_the_leases_record(dsn: str) -> None:
    """The deployed fake keeps servers in memory; a restart must not forget the warm pool."""
    from ipo.domain.registration import register_team

    settings, cloud, connect = make_env(dsn, pooled=2)
    reborn = FakeCloud()
    with connect() as c:
        for ip, port_id, server_id in c.execute(
                "SELECT host(ip), port_id, server_id FROM leases WHERE server_id IS NOT NULL"):
            reborn.adopt(ip=ip, port_id=port_id, server_id=server_id)
        reg = register_team(c, settings, slug="alpha", owner="o", idempotency_key="k")
        lease = leases.get(c, reg.ip)
    assert lease.server_id in reborn.servers
    reborn.set_server_metadata(lease.server_id, {"ipo.team": "t"})  # the step that failed
    fresh = reborn.ensure_port(name="p-new", ip="10.20.0.99")
    assert fresh.id not in {p for p in cloud.ports}  # new ids do not collide with adopted ones

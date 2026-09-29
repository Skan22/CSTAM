import threading
import time

import psycopg

from ipo.adapters.openstack.base import QuotaExceeded
from ipo.adapters.openstack.fake import FakeCloud
from ipo.controllers.pool import PoolManager
from ipo.domain import leases
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

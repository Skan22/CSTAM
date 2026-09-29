"""Shared builders for tests that need a seeded pool and a fake cloud."""

from collections.abc import Callable

import psycopg

from ipo.adapters.openstack.fake import FakeCloud
from ipo.controllers.pool import PoolManager
from ipo.domain import leases
from ipo.settings import Settings

ADDRS = [f"10.20.0.{i}" for i in range(10, 30)]


def make_env(
    dsn: str, *, addresses: int = 20, pooled: int = 0, **overrides: object
) -> tuple[Settings, FakeCloud, Callable[[], psycopg.Connection]]:
    settings = Settings(**{"reserve_free_ips": 0, "quarantine_seconds": 0, **overrides})  # type: ignore[arg-type]
    cloud = FakeCloud()

    def connect() -> psycopg.Connection:
        return psycopg.connect(dsn, autocommit=True)

    with connect() as c:
        leases.seed(c, ADDRS[:addresses])
    if pooled:
        PoolManager(cloud, settings, connect).reconcile(target=pooled)
    return settings, cloud, connect


class FakeGateways:
    """Stands in for the two gateways acking a config version that contains the route."""

    def __init__(self) -> None:
        self.waited: list[str] = []
        self.fail: Exception | None = None

    def wait_for_route(self, conn: psycopg.Connection, host: str, timeout: float) -> None:
        self.waited.append(host)
        if self.fail:
            raise self.fail


class FakeProber:
    def __init__(self) -> None:
        self.probed: list[str] = []
        self.fail: Exception | None = None

    def probe(self, host: str, timeout: float) -> None:
        self.probed.append(host)
        if self.fail:
            raise self.fail

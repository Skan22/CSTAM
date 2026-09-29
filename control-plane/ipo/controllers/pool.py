"""Warm pool: keeps `target` sandbox VMs booted and parked on reserved addresses."""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg

from ipo.adapters.openstack.base import (
    TAG,
    Cloud,
    CloudError,
    QuotaExceeded,
    port_name,
    server_name,
)
from ipo.domain import leases
from ipo.domain.teardown import release_resources
from ipo.settings import Settings

MAX_BACKOFF = 300.0


@dataclass(frozen=True)
class PoolReport:
    created: int
    failed: int
    backoff_seconds: float = 0.0


class PoolManager:
    def __init__(
        self, cloud: Cloud, settings: Settings, connect: Callable[[], psycopg.Connection],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cloud = cloud
        self._settings = settings
        self._connect = connect
        self._clock = clock
        self._blocked_until = 0.0
        self._backoff = 0.0
        self._lock = threading.Lock()

    def reconcile(self, target: int | None = None) -> PoolReport:
        target = self._settings.pool_target if target is None else target
        remaining = self._blocked_until - self._clock()
        if remaining > 0:
            return PoolReport(0, 0, remaining)

        with self._connect() as conn:
            unfinished = [
                r[0] for r in conn.execute(
                    "SELECT host(ip) FROM leases WHERE state = 'pooled' AND server_id IS NULL"
                    " ORDER BY ip")
            ]
            pooled = leases.counts(conn).get("pooled", 0)
            fresh: list[str] = []
            for _ in range(max(0, target - pooled)):
                lease = leases.reserve_free(conn)
                if lease is None:
                    break
                fresh.append(lease.ip)

        work = unfinished + fresh
        if not work:
            return PoolReport(0, 0)
        with ThreadPoolExecutor(max_workers=self._settings.max_parallel_boots) as ex:
            outcomes = list(ex.map(self._provision, work))

        quota_hit = "quota" in outcomes
        if quota_hit:
            self._backoff = min(max(self._backoff * 2, 5.0), MAX_BACKOFF)
            self._blocked_until = self._clock() + self._backoff
        elif all(o == "ok" for o in outcomes):
            self._backoff = 0.0
        return PoolReport(
            created=outcomes.count("ok"),
            failed=len(outcomes) - outcomes.count("ok"),
            backoff_seconds=self._backoff if quota_hit else 0.0,
        )

    def _provision(self, ip: str) -> str:
        with self._connect() as conn:
            try:
                port = self._cloud.ensure_port(name=port_name(ip), ip=ip, tags=(TAG,))
                server = self._cloud.ensure_server(
                    name=server_name(ip), port_id=port.id, tags=(TAG,),
                    metadata={"ipo.role": "sandbox"},
                )
                leases.attach_resources(conn, ip, port_id=port.id, server_id=server.id)
                return "ok"
            except CloudError as e:
                release_resources(
                    conn, self._cloud, ip, quarantine_seconds=self._settings.quarantine_seconds
                )
                return "quota" if isinstance(e, QuotaExceeded) else "error"

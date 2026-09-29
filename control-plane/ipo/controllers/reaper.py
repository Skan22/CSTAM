"""Reaper: every 10 s, queue teardown for expired leases and free quarantined addresses."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from ipo.controllers.workers import run_leader
from ipo.domain import leases
from ipo.domain.teardown import TeamBusy, enqueue_teardown

log = logging.getLogger("ipo.reaper")


@dataclass
class ReaperReport:
    torn_down: int = 0
    freed: list[str] = field(default_factory=list)


class Reaper:
    def __init__(self, connect: Callable[[], psycopg.Connection], *, interval: float = 10.0):
        self.connect = connect
        self.interval = interval

    def run_once(self) -> ReaperReport:
        report = ReaperReport()
        with self.connect() as conn:
            report.freed = leases.release_expired_quarantine(conn)
            for lease in leases.expired(conn):
                assert lease.team_id
                try:
                    with conn.transaction():
                        jobs = conn.execute(
                            "SELECT count(*) FROM jobs WHERE kind = 'teardown' AND team_id = %s"
                            " AND state IN ('queued', 'running')", (lease.team_id,)
                        ).fetchone()
                        if jobs and jobs[0]:
                            continue  # already on its way out
                        if enqueue_teardown(conn, lease.team_id, reason="expired", actor="reaper"):
                            report.torn_down += 1
                except TeamBusy:
                    log.info("lease %s expired while its team is still registering", lease.ip)
        return report

    def run(self, stop: threading.Event) -> None:
        run_leader(self.connect, "reaper", stop, self.interval, self.run_once)

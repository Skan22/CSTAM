"""Assembles the saga runner that handles every job kind, and the loops that drive controllers."""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg

from ipo.controllers import register_saga
from ipo.controllers.register_saga import Deps
from ipo.controllers.saga import Hook, SagaRunner
from ipo.controllers.teardown_saga import teardown_steps
from ipo.domain import leases

log = logging.getLogger("ipo.workers")


def build_runner(
    connect: Callable[[], psycopg.Connection], deps: Deps, *, worker_id: str,
    hook: Hook | None = None, **kwargs: Any,
) -> SagaRunner:
    """A runner for registration and teardown jobs."""
    return register_saga.build_runner(
        connect, deps, worker_id=worker_id, hook=hook, extra=teardown_steps(deps), **kwargs)


class JobWakeups:
    """Turns the database's `ipo_jobs` notifications into permits for idle workers.

    Without it every idle worker polls the queue on a timer, which at a few dozen workers is
    hundreds of connections a second doing nothing. Here one thread listens, and each queued job
    wakes exactly one worker at once. Once a second it also hands out a permit with no
    notification, so jobs scheduled for later (a step's retry back-off) and notifications lost to
    a reconnect are still found.
    """

    def __init__(self, tick: float = 1.0) -> None:
        self._permits = threading.Semaphore(0)
        self.tick = tick

    def wait(self, timeout: float) -> bool:
        return self._permits.acquire(timeout=timeout)

    def run(self, connect: Callable[[], psycopg.Connection], stop: threading.Event) -> None:
        conn = connect()
        try:
            conn.execute("LISTEN ipo_jobs")
            last = time.monotonic()
            while not stop.is_set():
                for _ in conn.notifies(timeout=min(self.tick, 0.5), stop_after=64):
                    self._permits.release()
                    last = time.monotonic()
                if time.monotonic() - last >= self.tick:
                    self._permits.release()
                    last = time.monotonic()
        finally:
            conn.close()
            for _ in range(1000):  # let every idle worker see that it is time to stop
                self._permits.release()


def run_worker(runner: SagaRunner, stop: threading.Event, idle: float = 0.2,
               wakeups: JobWakeups | None = None) -> None:
    """Run jobs until `stop`. Idle, wait for a wake-up (or poll every `idle` seconds without
    one); after a job, look straight away for the next."""
    while not stop.is_set():
        try:
            if runner.run_once() is not None:
                continue
        except Exception:
            log.exception("worker iteration failed")
        if wakeups is not None:
            wakeups.wait(5.0)
        else:
            stop.wait(idle)


def run_leader(
    connect: Callable[[], psycopg.Connection], name: str, stop: threading.Event,
    interval: float, tick: Callable[[], object],
) -> None:
    """Run `tick` every `interval` seconds, but only while holding the `name` leader lock."""
    lead = connect()
    try:
        while not stop.is_set():
            if leases.try_lead(lead, name):
                try:
                    tick()
                except Exception:
                    log.exception("%s tick failed", name)
            stop.wait(interval)
    finally:
        lead.close()

"""Assembles the saga runner that handles every job kind, and the loops that drive controllers."""

import logging
import threading
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


def run_worker(runner: SagaRunner, stop: threading.Event, idle: float = 0.2) -> None:
    while not stop.is_set():
        try:
            if runner.run_once() is None:
                stop.wait(idle)
        except Exception:
            log.exception("worker iteration failed")
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

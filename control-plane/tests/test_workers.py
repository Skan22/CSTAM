"""Idle workers sleep until a job is queued, and wake at once when one is."""

import threading
import time

from ipo.controllers.register_saga import Deps
from ipo.controllers.workers import JobWakeups, build_runner, run_worker
from ipo.domain.registration import register_team
from tests.helpers import FakeGateways, FakeProber, make_env


def test_a_queued_job_wakes_an_idle_worker_immediately(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn, pooled=2)
    wakeups = JobWakeups(tick=30.0)  # no periodic permit in the test: only a notification wakes
    stop = threading.Event()
    threads = [threading.Thread(target=wakeups.run, args=(connect, stop), daemon=True)]
    for i in range(4):
        runner = build_runner(connect, Deps(cloud, FakeGateways(), FakeProber(), settings),
                              worker_id=f"w{i}")
        threads.append(threading.Thread(target=run_worker, args=(runner, stop),
                                        kwargs={"wakeups": wakeups}, daemon=True))
    for t in threads:
        t.start()
    time.sleep(0.5)  # everyone is listening or waiting
    try:
        began = time.monotonic()
        with connect() as c:
            register_team(c, settings, slug="alpha", owner="o", idempotency_key="k")
        deadline = began + 5
        while time.monotonic() < deadline:
            with connect() as c:
                state = c.execute("SELECT state FROM jobs").fetchone()
            if state == ("succeeded",):
                break
            time.sleep(0.02)
        took = time.monotonic() - began
        assert state == ("succeeded",), state
        assert took < 1.5, f"the job waited {took:.2f}s for a worker"
    finally:
        stop.set()
        for t in threads:
            t.join(5)
    assert not any(t.is_alive() for t in threads)  # nobody is left sleeping out a long wait


def test_the_periodic_permit_finds_work_whose_notification_was_missed(dsn: str) -> None:
    settings, cloud, connect = make_env(dsn, pooled=1)
    with connect() as c:  # queued before anyone listened
        register_team(c, settings, slug="alpha", owner="o", idempotency_key="k")
    wakeups = JobWakeups(tick=0.2)
    stop = threading.Event()
    runner = build_runner(connect, Deps(cloud, FakeGateways(), FakeProber(), settings),
                          worker_id="w")
    # the worker's first look finds the job; a second job queued silently is found by the tick
    threads = [threading.Thread(target=wakeups.run, args=(connect, stop), daemon=True),
               threading.Thread(target=run_worker, args=(runner, stop),
                                kwargs={"wakeups": wakeups}, daemon=True)]
    for t in threads:
        t.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connect() as c:
                if c.execute("SELECT state FROM jobs").fetchone() == ("succeeded",):
                    return
            time.sleep(0.05)
        raise AssertionError("the queued job was never run")
    finally:
        stop.set()
        for t in threads:
            t.join(5)

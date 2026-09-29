"""Generic saga runner.

Each job is a list of steps. A step's progress is recorded in `saga_steps` as it happens, so:
- a worker that dies is replaced by another that resumes from the last completed step;
- a step that keeps failing triggers the undo of every step that started, in reverse order;
- steps must be idempotent, because a resume may run one a second time.
"""

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from ipo import metrics


class WorkerCrash(BaseException):
    """Simulates the process being killed. Deliberately not an Exception, so nothing catches it."""


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    team_id: str | None
    payload: dict[str, Any]
    attempts: int


class Step(Protocol):
    name: str

    def run(self, conn: psycopg.Connection, job: Job) -> Any: ...

    def undo(self, conn: psycopg.Connection, job: Job) -> None: ...


Hook = Callable[[str, str], None]


class SagaRunner:
    def __init__(
        self, connect: Callable[[], psycopg.Connection], steps: Mapping[str, Sequence[Step]], *,
        worker_id: str, stale_seconds: float = 60.0, max_step_attempts: int = 3,
        max_job_attempts: int = 5, base_backoff: float = 0.2,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random, hook: Hook | None = None,
    ) -> None:
        self._connect = connect
        self._steps = steps
        self._worker_id = worker_id
        self._stale = stale_seconds
        self._max_step_attempts = max_step_attempts
        self._max_job_attempts = max_job_attempts
        self._base_backoff = base_backoff
        self._sleep = sleep
        self._jitter = jitter
        self._hook = hook or (lambda event, step: None)

    # -- claiming
    def claim(self, conn: psycopg.Connection) -> Job | None:
        """Take one runnable job. A 'running' job whose lock is stale belongs to a dead worker."""
        row = conn.execute(
            "WITH pick AS (SELECT id FROM jobs"
            "  WHERE (state = 'queued' AND run_after <= now())"
            "     OR (state = 'running' AND locked_at <= now() - make_interval(secs => %s))"
            "  ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)"
            " UPDATE jobs SET state = 'running', locked_by = %s, locked_at = now(),"
            "   attempts = attempts + 1 FROM pick WHERE jobs.id = pick.id"
            " RETURNING jobs.id::text, kind, team_id::text, payload, attempts",
            (self._stale, self._worker_id),
        ).fetchone()
        return Job(*row) if row else None

    def run_once(self) -> str | None:
        """Run one job to a final outcome: succeeded, compensated or failed. None if idle."""
        with self._connect() as conn:
            job = self.claim(conn)
            return self.run_job(conn, job) if job else None

    # -- running
    def run_job(self, conn: psycopg.Connection, job: Job) -> str:
        outcome = self._run_job(conn, job)
        metrics.SAGA_JOBS.labels(job.kind, outcome).inc()
        return outcome

    def _run_job(self, conn: psycopg.Connection, job: Job) -> str:
        steps = self._steps[job.kind]
        if job.attempts > self._max_job_attempts:
            return self._compensate(conn, job, steps, "abandoned after repeated worker crashes")
        for step in steps:
            if self._status(conn, job, step.name) == "done":
                continue
            self._heartbeat(conn, job)
            conn.execute(
                "INSERT INTO saga_steps (job_id, step, status) VALUES (%s, %s, 'running')"
                " ON CONFLICT (job_id, step) DO UPDATE SET status = 'running',"
                "   started_at = now(), finished_at = NULL", (job.id, step.name))
            self._hook("before", step.name)
            started = time.monotonic()
            try:
                result = self._retrying(partial(step.run, conn, job))
            except Exception as e:
                self._record(conn, job, step.name, "failed", {"error": str(e)})
                metrics.SAGA_STEP_DURATION.labels(step.name).observe(time.monotonic() - started)
                return self._compensate(conn, job, steps, f"{step.name}: {e}")
            metrics.SAGA_STEP_DURATION.labels(step.name).observe(time.monotonic() - started)
            self._record(conn, job, step.name, "done", result)
            self._hook("after", step.name)
        conn.execute(
            "UPDATE jobs SET state = 'succeeded', locked_by = NULL WHERE id = %s", (job.id,))
        return "succeeded"

    def _compensate(
        self, conn: psycopg.Connection, job: Job, steps: Sequence[Step], reason: str
    ) -> str:
        for step in reversed(steps):
            status = self._status(conn, job, step.name)
            if status in (None, "undone"):
                continue
            self._heartbeat(conn, job)
            try:
                self._retrying(partial(step.undo, conn, job))
            except Exception as e:
                conn.execute(
                    "UPDATE jobs SET state = 'failed', locked_by = NULL, last_error = %s"
                    " WHERE id = %s", (f"undo of {step.name} failed: {e}; after: {reason}", job.id))
                return "failed"
            self._record(conn, job, step.name, "undone", None)
        conn.execute(
            "UPDATE jobs SET state = 'compensated', locked_by = NULL, last_error = %s"
            " WHERE id = %s", (reason, job.id))
        return "compensated"

    # -- helpers
    def _retrying[T](self, fn: Callable[[], T]) -> T:
        last: Exception | None = None
        for attempt in range(self._max_step_attempts):
            try:
                return fn()
            except Exception as e:
                last = e
                if attempt + 1 < self._max_step_attempts:
                    self._sleep(self._base_backoff * 2**attempt * (0.5 + self._jitter()))
        assert last is not None
        raise last

    @staticmethod
    def _status(conn: psycopg.Connection, job: Job, step: str) -> str | None:
        row = conn.execute(
            "SELECT status FROM saga_steps WHERE job_id = %s AND step = %s", (job.id, step)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _record(
        conn: psycopg.Connection, job: Job, step: str, status: str, result: Any
    ) -> None:
        conn.execute(
            "UPDATE saga_steps SET status = %s, finished_at = now(),"
            " result = COALESCE(%s, result) WHERE job_id = %s AND step = %s",
            (status, Jsonb(result) if result is not None else None, job.id, step))

    @staticmethod
    def _heartbeat(conn: psycopg.Connection, job: Job) -> None:
        conn.execute("UPDATE jobs SET locked_at = now() WHERE id = %s", (job.id,))

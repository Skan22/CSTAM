"""The teardown saga, shared by the reaper (expiry) and `DELETE /v1/teams/{id}`.

Order matters: traffic stops first (route removed, gateways ack), then the VM and port go, then
the address is quarantined. Teardown only moves forward, so no step has an undo; a job that
ends up failed is picked up by the reconciler, which calls the same domain functions.
"""

import logging
from typing import Any

import psycopg

from ipo.controllers.register_saga import Deps
from ipo.controllers.saga import Job, Step
from ipo.domain import teardown
from ipo.domain.teardown import KIND

log = logging.getLogger("ipo.teardown")

STEP_NAMES = ["drain", "await_route_removal", "release", "finalize"]


class _Forward:
    name: str

    def __init__(self, deps: Deps) -> None:
        self.d = deps

    def undo(self, conn: psycopg.Connection, job: Job) -> None:
        return None


class Drain(_Forward):
    name = "drain"

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        assert job.team_id
        teardown.drain_team(conn, job.team_id)
        return None


class AwaitRouteRemoval(_Forward):
    name = "await_route_removal"

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        try:
            self.d.gateways.wait_for_route_removed(
                conn, job.payload["host"], self.d.converge_timeout)
        except TimeoutError as exc:
            # A gateway that is down must not pin an address forever. Quarantine plus the
            # reconciler's re-push bound the exposure, so carry on and say so.
            log.warning("teardown of %s: %s; continuing", job.payload["slug"], exc)
            return {"acked": False, "error": str(exc)}
        return {"acked": True}


class Release(_Forward):
    name = "release"

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        assert job.team_id
        teardown.release_team(conn, self.d.cloud, job.team_id, job.payload.get("ip"),
                              quarantine_seconds=self.d.settings.quarantine_seconds)
        return None


class Finalize(_Forward):
    name = "finalize"

    def run(self, conn: psycopg.Connection, job: Job) -> Any:
        assert job.team_id
        teardown.finalize_team(conn, job.team_id)
        return None


def teardown_steps(deps: Deps) -> dict[str, list[Step]]:
    steps: list[Step] = [Drain(deps), AwaitRouteRemoval(deps), Release(deps), Finalize(deps)]
    assert [s.name for s in steps] == STEP_NAMES
    return {KIND: steps}

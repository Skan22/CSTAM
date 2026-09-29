"""Reconciler: compares the database, OpenStack and the gateways and repairs drift.

It runs every 60 s and repairs only what has been wrong for longer than a grace period, so it
never races a saga that is legitimately mid-flight. Every repair is idempotent and counted in
`ipo_reconciler_repairs_total{rule}`.
"""

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import psycopg

from ipo import metrics
from ipo.adapters.openstack.base import Cloud, Port, Server
from ipo.controllers.compiler import ConfigVersion
from ipo.controllers.workers import run_leader
from ipo.domain import events, leases
from ipo.domain.registration import KIND as REGISTER_KIND
from ipo.domain.teardown import (
    TeamBusy,
    audit,
    drain_team,
    enqueue_teardown,
    finalize_team,
    release_resources,
)
from ipo.settings import Settings

log = logging.getLogger("ipo.reconciler")

RULES = ("orphan_vm", "orphan_port", "vm_gone", "stale_route", "stuck_lease", "stuck_team",
         "gateway_behind")


class Reconciler:
    def __init__(
        self, cloud: Cloud, settings: Settings, connect: Callable[[], psycopg.Connection], *,
        repush: Callable[[ConfigVersion], object] | None = None, grace_seconds: float = 30.0,
        interval: float = 60.0, now: Callable[[], float] = time.time,
    ) -> None:
        self.cloud = cloud
        self.settings = settings
        self.connect = connect
        self.repush = repush
        self.grace = grace_seconds
        self.interval = interval
        self.now = now

    def run_once(self) -> dict[str, int]:
        repairs: Counter[str] = Counter({r: 0 for r in RULES})
        with self.connect() as conn:
            # Cloud is read once, first, then the database: a resource created between the two
            # reads is already recorded, so it is never mistaken for an orphan.
            ports, servers = self.cloud.list_ports(), self.cloud.list_servers()
            rows = conn.execute(
                "SELECT host(ip), state::text, team_id::text, server_id,"
                " extract(epoch FROM clock_timestamp() - updated_at) FROM leases").fetchall()
            state = {ip: (st, team, sid, float(age)) for ip, st, team, sid, age in rows}
            self._orphans(servers, ports, state, repairs)
            self._vm_gone(conn, servers, state, repairs)
            self._stale_routes(conn, repairs)
            self._stuck_leases(conn, state, repairs)
            self._stuck_teams(conn, repairs)
            self._gateways(conn, repairs)
        for rule, n in repairs.items():
            if n:
                metrics.RECONCILER_REPAIRS.labels(rule).inc(n)
                log.warning("reconciler repaired %d x %s", n, rule)
        return dict(repairs)

    # -- rules
    def _old(self, created_at: float) -> bool:
        return self.now() - created_at >= self.grace

    def _orphans(
        self, servers: list[Server], ports: list[Port],
        state: dict[str, tuple[str, str | None, str | None, float]], repairs: Counter[str],
    ) -> None:
        def unowned(ip: str | None) -> bool:
            # free, quarantined or unknown: nothing should be running there. draining is a
            # teardown in progress and is left to it.
            return ip is None or state.get(ip, ("free",))[0] in ("free", "quarantined")

        port_ip = {p.id: p.ip for p in ports}
        for s in servers:
            if unowned(port_ip.get(s.port_id)) and self._old(s.created_at):
                self.cloud.delete_server(s.id)
                repairs["orphan_vm"] += 1
        for p in ports:
            if unowned(p.ip) and self._old(p.created_at):
                self.cloud.delete_port(p.id)
                repairs["orphan_port"] += 1

    def _vm_gone(
        self, conn: psycopg.Connection, servers: list[Server],
        state: dict[str, tuple[str, str | None, str | None, float]], repairs: Counter[str],
    ) -> None:
        alive = {s.id for s in servers}
        for ip, (st, team, server_id, age) in state.items():
            if st != "leased" or not server_id or server_id in alive or age < self.grace:
                continue
            assert team
            with conn.transaction():
                conn.execute("UPDATE teams SET state = 'failed' WHERE id = %s AND state = 'active'",
                             (team,))
                audit(conn, "reconciler", "team.vm_gone", team, ip=ip, server_id=server_id)
                events.emit(conn, "alert.vm_gone", team_id=team, ip=ip)
                try:
                    enqueue_teardown(conn, team, reason="vm_gone", actor="reconciler")
                except TeamBusy:
                    continue  # the registration saga will notice the missing VM itself
            repairs["vm_gone"] += 1

    def _stale_routes(self, conn: psycopg.Connection, repairs: Counter[str]) -> None:
        with conn.transaction():
            rows = conn.execute(
                "DELETE FROM routes r USING teams t WHERE t.id = r.team_id"
                " AND r.created_at <= now() - make_interval(secs => %s)"
                " AND (t.state IN ('draining', 'deleted', 'failed')"
                "      OR (t.state = 'pending' AND NOT EXISTS (SELECT 1 FROM jobs j"
                "          WHERE j.team_id = t.id AND j.kind = %s"
                "            AND j.state IN ('queued', 'running'))))"
                " RETURNING r.host", (self.grace, REGISTER_KIND)).fetchall()
            for (host,) in rows:
                conn.execute("SELECT pg_notify('ipo_routes_changed', %s)", (host,))
        repairs["stale_route"] += len(rows)

    def _stuck_leases(
        self, conn: psycopg.Connection,
        state: dict[str, tuple[str, str | None, str | None, float]], repairs: Counter[str],
    ) -> None:
        for ip, (st, _team, _srv, age) in state.items():
            if st == "draining" and age >= self.grace:
                release_resources(conn, self.cloud, ip,
                                  quarantine_seconds=self.settings.quarantine_seconds)
                repairs["stuck_lease"] += 1
        freed = leases.release_expired_quarantine(conn)
        repairs["stuck_lease"] += len(freed)

    def _stuck_teams(self, conn: psycopg.Connection, repairs: Counter[str]) -> None:
        """A draining team whose teardown job died: finish it here, then close the job."""
        rows = conn.execute(
            "SELECT t.id::text, (SELECT host(ip) FROM leases l WHERE l.team_id = t.id"
            "                    AND l.state IN ('leased', 'draining'))"
            " FROM teams t WHERE t.state = 'draining' AND NOT EXISTS (SELECT 1 FROM jobs j"
            "   WHERE j.team_id = t.id AND j.kind = 'teardown'"
            "     AND ((j.state = 'running' AND j.locked_at > now() - make_interval(secs => %s))"
            "       OR (j.state = 'queued' AND j.created_at > now() - make_interval(secs => %s))))",
            (self.grace, self.grace)).fetchall()
        for team_id, ip in rows:
            drain_team(conn, team_id)
            if ip:
                release_resources(conn, self.cloud, ip,
                                  quarantine_seconds=self.settings.quarantine_seconds)
            finalize_team(conn, team_id, actor="reconciler")
            conn.execute(
                "UPDATE jobs SET state = 'succeeded', locked_by = NULL,"
                " last_error = 'completed by reconciler'"
                " WHERE kind = 'teardown' AND team_id = %s AND state IN ('queued', 'running')",
                (team_id,))
            repairs["stuck_team"] += 1

    def _gateways(self, conn: psycopg.Connection, repairs: Counter[str]) -> None:
        if self.repush is None:
            return
        row: Any = conn.execute(
            "SELECT version, sha256, signature, body FROM config_versions"
            " WHERE status IN ('pending', 'live')"
            "   AND created_at <= now() - make_interval(secs => %s)"
            " ORDER BY version DESC LIMIT 1", (self.grace,)).fetchone()
        if row is None:
            return
        behind = conn.execute(
            "SELECT count(*) FROM unnest(%s::text[]) AS gw(name)"
            " LEFT JOIN gateway_status g ON g.gateway = gw.name"
            " WHERE COALESCE(g.live_version, 0) < %s",
            (list(self.settings.gateways), row[0])).fetchone()
        if behind and behind[0]:
            self.repush(ConfigVersion(*row))
            repairs["gateway_behind"] += 1

    def run(self, stop: threading.Event) -> None:
        run_leader(self.connect, "reconciler", stop, self.interval, self.run_once)

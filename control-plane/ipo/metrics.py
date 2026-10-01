"""Prometheus metrics. One module so the API's /metrics and every controller share a registry."""

from collections.abc import Sequence

import psycopg
from prometheus_client import Counter, Gauge, Histogram

from ipo.domain.gateways import masters as gateway_masters

RECONCILER_REPAIRS = Counter(
    "ipo_reconciler_repairs_total", "Drift repaired by the reconciler", ["rule"])

IP_ADDRESSES = Gauge("ipo_ip_addresses", "Addresses by lease state", ["state"])
POOL_SIZE = Gauge("ipo_pool_size", "Warm VMs ready to be claimed")
POOL_TARGET = Gauge("ipo_pool_target", "Configured warm pool size")
TEAMS = Gauge("ipo_teams", "Teams by state", ["state"])
CONFIG_LIVE_VERSION = Gauge("ipo_config_live_version", "Config version each gateway runs",
                            ["gateway"])
GATEWAY_MASTERS = Gauge(
    "ipo_gateway_masters",
    "Gateways whose fresh heartbeat says MASTER; anything but 1 is wrong, 2 is a split brain")
SPLIT_BRAINS = Counter("ipo_split_brain_total", "Times two gateways were seen as MASTER at once")
API_DURATION = Histogram(
    "ipo_api_request_duration_seconds", "Time to first response byte",
    ["method", "route", "status"])

TEAM_REQUESTS = Gauge(
    "ipo_team_requests_5m", "Requests a team's host served in the last five minutes, by status "
    "class", ["team", "class"])
TEAM_LATENCY_MS = Gauge(
    "ipo_team_latency_ms_5m", "Mean time to serve a team's requests in the last five minutes",
    ["team"])
_team_series: set[str] = set()  # teams with series exported, so a deleted team's go away

SAGA_STEP_DURATION = Histogram(
    "ipo_saga_step_duration_seconds", "Time a saga step takes, retries included", ["step"])
SAGA_JOBS = Counter(
    "ipo_saga_jobs_total", "Jobs that reached a final outcome", ["kind", "outcome"])

PROVISION_DURATION = Histogram(
    "ipo_provision_duration_seconds", "Registration request to active team", ["path"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300))

LEASE_STATES = ("free", "pooled", "leased", "draining", "quarantined")
TEAM_STATES = ("pending", "active", "draining", "deleted", "failed")


def refresh_gauges(conn: psycopg.Connection, *, pool_target: int,
                   gateways: Sequence[str] = (), fresh_seconds: float = 15) -> None:
    """Gauges are read from the database at scrape time, so every replica reports the truth."""
    lease_counts: dict[str, int] = dict(
        conn.execute("SELECT state::text, count(*) FROM leases GROUP BY 1"))
    for s in LEASE_STATES:
        IP_ADDRESSES.labels(s).set(lease_counts.get(s, 0))
    POOL_SIZE.set(lease_counts.get("pooled", 0))
    POOL_TARGET.set(pool_target)
    team_counts: dict[str, int] = dict(conn.execute("SELECT state, count(*) FROM teams GROUP BY 1"))
    for s in TEAM_STATES:
        TEAMS.labels(s).set(team_counts.get(s, 0))
    GATEWAY_MASTERS.set(len(gateway_masters(conn, gateways, fresh_seconds)))
    for gateway, version in conn.execute("SELECT gateway, live_version FROM gateway_status"):
        CONFIG_LIVE_VERSION.labels(gateway).set(version or 0)
    _refresh_team_traffic(conn)


def _refresh_team_traffic(conn: psycopg.Connection) -> None:
    """Per-team gauges over the last five minutes, for teams that still have a route, so the
    label set follows the live teams instead of growing with every team ever made."""
    rows = conn.execute(
        "SELECT t.slug, coalesce(sum(b.s2xx), 0), coalesce(sum(b.s3xx), 0),"
        " coalesce(sum(b.s4xx), 0), coalesce(sum(b.s5xx), 0), coalesce(sum(b.requests), 0),"
        " coalesce(sum(b.duration_ms), 0)"
        " FROM routes r JOIN teams t ON t.id = r.team_id"
        " LEFT JOIN traffic_buckets b ON b.host = r.host"
        " AND b.bucket >= date_trunc('minute', now()) - interval '5 minutes'"
        " GROUP BY t.slug").fetchall()
    seen = set()
    for slug, s2, s3, s4, s5, total, ms in rows:
        seen.add(slug)
        for cls, n in (("2xx", s2), ("3xx", s3), ("4xx", s4), ("5xx", s5)):
            TEAM_REQUESTS.labels(slug, cls).set(float(n))
        TEAM_LATENCY_MS.labels(slug).set(float(ms) / float(total) if total else 0.0)
    for gone in _team_series - seen:
        for cls in ("2xx", "3xx", "4xx", "5xx"):
            TEAM_REQUESTS.remove(gone, cls)
        TEAM_LATENCY_MS.remove(gone)
    _team_series.clear()
    _team_series.update(seen)

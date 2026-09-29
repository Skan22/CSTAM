"""Prometheus metrics. One module so the API's /metrics and every controller share a registry."""

import psycopg
from prometheus_client import Counter, Gauge, Histogram

RECONCILER_REPAIRS = Counter(
    "ipo_reconciler_repairs_total", "Drift repaired by the reconciler", ["rule"])

IP_ADDRESSES = Gauge("ipo_ip_addresses", "Addresses by lease state", ["state"])
POOL_SIZE = Gauge("ipo_pool_size", "Warm VMs ready to be claimed")
POOL_TARGET = Gauge("ipo_pool_target", "Configured warm pool size")
TEAMS = Gauge("ipo_teams", "Teams by state", ["state"])
CONFIG_LIVE_VERSION = Gauge("ipo_config_live_version", "Config version each gateway runs",
                            ["gateway"])
API_DURATION = Histogram(
    "ipo_api_request_duration_seconds", "Time to first response byte",
    ["method", "route", "status"])

LEASE_STATES = ("free", "pooled", "leased", "draining", "quarantined")
TEAM_STATES = ("pending", "active", "draining", "deleted", "failed")


def refresh_gauges(conn: psycopg.Connection, *, pool_target: int) -> None:
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
    for gateway, version in conn.execute("SELECT gateway, live_version FROM gateway_status"):
        CONFIG_LIVE_VERSION.labels(gateway).set(version or 0)

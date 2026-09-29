"""Which gateways currently hold the VIP, as far as their heartbeats say."""

from collections.abc import Sequence

import psycopg


def masters(conn: psycopg.Connection, gateways: Sequence[str], fresh_seconds: float) -> list[str]:
    """Gateways whose latest heartbeat says MASTER and arrived within `fresh_seconds`.

    When several qualify, only those that have reported since the newest promotion count: a
    gateway that died as MASTER stops reporting, so the survivor taking over is not a split brain,
    while two gateways that both keep reporting MASTER are.
    """
    rows = conn.execute(
        "WITH m AS (SELECT gateway, last_heartbeat, vrrp_since FROM gateway_status"
        " WHERE vrrp_state = 'MASTER' AND gateway = ANY(%s)"
        " AND last_heartbeat > now() - make_interval(secs => %s))"
        " SELECT gateway FROM m WHERE (SELECT count(*) FROM m) = 1"
        " OR last_heartbeat >= (SELECT max(vrrp_since) FROM m) ORDER BY gateway",
        (list(gateways), fresh_seconds)).fetchall()
    return [r[0] for r in rows]

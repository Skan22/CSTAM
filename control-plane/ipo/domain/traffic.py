"""Per-team traffic: counters the gateway agents read from Traefik's access log.

Only hosts that have a route are stored, so scanners hitting the VIP with random Host headers
cannot grow the tables. Time is the server's, not the agent's.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ipo.domain import events

KEEP_BUCKET_HOURS = 26
KEEP_RECENT = 2000

_INSERT_BUCKETS = """
INSERT INTO traffic_buckets (bucket, gateway, host, requests, s2xx, s3xx, s4xx, s5xx, bytes,
                             duration_ms)
SELECT date_trunc('minute', now()), %(gw)s, r.host, sum(h.requests), sum(h.s2xx), sum(h.s3xx),
       sum(h.s4xx), sum(h.s5xx), sum(h.bytes), sum(h.duration_ms_sum)
FROM jsonb_to_recordset(%(hosts)s) AS h(host text, requests bigint, s2xx bigint, s3xx bigint,
                                        s4xx bigint, s5xx bigint, bytes bigint,
                                        duration_ms_sum bigint)
JOIN routes r ON r.host = lower(h.host)
GROUP BY r.host
ON CONFLICT (bucket, gateway, host) DO UPDATE SET
  requests = traffic_buckets.requests + EXCLUDED.requests,
  s2xx = traffic_buckets.s2xx + EXCLUDED.s2xx, s3xx = traffic_buckets.s3xx + EXCLUDED.s3xx,
  s4xx = traffic_buckets.s4xx + EXCLUDED.s4xx, s5xx = traffic_buckets.s5xx + EXCLUDED.s5xx,
  bytes = traffic_buckets.bytes + EXCLUDED.bytes,
  duration_ms = traffic_buckets.duration_ms + EXCLUDED.duration_ms
"""

_INSERT_RECENT = """
INSERT INTO traffic_recent (at, gateway, host, method, path, status, duration_ms)
SELECT h.at, %(gw)s, r.host, h.method, h.path, h.status, h.duration_ms
FROM jsonb_array_elements(%(recent)s) WITH ORDINALITY AS e(v, n),
     LATERAL jsonb_to_record(e.v) AS h(at timestamptz, host text, method text, path text,
                                       status int, duration_ms int)
JOIN routes r ON r.host = lower(h.host)
ORDER BY h.at, e.n
"""


def ingest(conn: psycopg.Connection, gateway: str, hosts: Sequence[Mapping[str, Any]],
           recent: Sequence[Mapping[str, Any]]) -> None:
    """Add a gateway's report and announce it. `hosts` and `recent` are plain dicts."""
    with conn.transaction():
        conn.execute(_INSERT_BUCKETS, {"gw": gateway, "hosts": Jsonb(list(hosts))})
        routed = conn.execute(
            "SELECT r.host, sum(h.requests)::bigint"
            " FROM jsonb_to_recordset(%s) AS h(host text, requests bigint)"
            " JOIN routes r ON r.host = lower(h.host) GROUP BY r.host ORDER BY r.host",
            (Jsonb(list(hosts)),)).fetchall()
        if recent:
            conn.execute(_INSERT_RECENT, {"gw": gateway, "recent": Jsonb(list(recent))})
            conn.execute("DELETE FROM traffic_recent"
                         " WHERE id <= (SELECT max(id) FROM traffic_recent) - %s", (KEEP_RECENT,))
        conn.execute("DELETE FROM traffic_buckets"
                     " WHERE bucket < now() - make_interval(hours => %s)", (KEEP_BUCKET_HOURS,))
        if routed:
            events.emit(conn, "traffic.batch", gateway=gateway,
                        requests=sum(n for _, n in routed), hosts=[h for h, _ in routed])


def summary(conn: psycopg.Connection, window_minutes: int, *,
            team_id: str | None = None) -> dict[str, Any]:
    """Per-team rows (busiest first), totals and a per-minute series over the window."""
    scope = " AND r.team_id = %(team)s" if team_id else ""
    args = {"mins": window_minutes, "team": team_id}
    rows = conn.execute(
        "SELECT r.team_id::text, t.slug, r.host,"
        " coalesce(sum(b.requests), 0), coalesce(sum(b.s2xx), 0), coalesce(sum(b.s3xx), 0),"
        " coalesce(sum(b.s4xx), 0), coalesce(sum(b.s5xx), 0), coalesce(sum(b.bytes), 0),"
        " coalesce(sum(b.duration_ms), 0), max(b.bucket)"
        " FROM routes r JOIN teams t ON t.id = r.team_id"
        " LEFT JOIN traffic_buckets b ON b.host = r.host"
        " AND b.bucket >= date_trunc('minute', now()) - make_interval(mins => %(mins)s)"
        f" WHERE true{scope} GROUP BY r.team_id, t.slug, r.host"
        " ORDER BY 4 DESC, t.slug", args).fetchall()
    teams = [{"team_id": r[0], "slug": r[1], "host": r[2], "requests": int(r[3]), "s2xx": int(r[4]),
              "s3xx": int(r[5]), "s4xx": int(r[6]), "s5xx": int(r[7]), "bytes": int(r[8]),
              "avg_ms": float(r[9]) / float(r[3]) if r[3] else 0.0,
              "last_seen": r[10]} for r in rows]
    series = conn.execute(
        "SELECT b.bucket, sum(b.requests), sum(b.s5xx) FROM traffic_buckets b"
        " JOIN routes r ON r.host = b.host"
        " WHERE b.bucket >= date_trunc('minute', now()) - make_interval(mins => %(mins)s)"
        f"{scope} GROUP BY b.bucket ORDER BY b.bucket", args).fetchall()
    keys = ("requests", "s2xx", "s3xx", "s4xx", "s5xx", "bytes")
    totals = {k: sum(t[k] for t in teams) for k in keys}
    return {"window_minutes": window_minutes, "totals": totals, "teams": teams,
            "series": [{"at": s[0], "requests": int(s[1]), "errors": int(s[2])} for s in series]}


def recent(conn: psycopg.Connection, limit: int, *, host: str | None = None,
           team_id: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT q.at, q.gateway, q.host, t.slug, q.method, q.path, q.status, q.duration_ms"
        " FROM traffic_recent q JOIN routes r ON r.host = q.host JOIN teams t ON t.id = r.team_id"
        " WHERE (%(host)s::text IS NULL OR q.host = %(host)s)"
        " AND (%(team)s::uuid IS NULL OR r.team_id = %(team)s)"
        " ORDER BY q.id DESC LIMIT %(n)s", {"host": host, "team": team_id, "n": limit}).fetchall()
    return [{"at": r[0], "gateway": r[1], "host": r[2], "slug": r[3], "method": r[4], "path": r[5],
             "status": r[6], "duration_ms": r[7]} for r in rows]

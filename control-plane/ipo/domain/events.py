"""Fire-and-forget events for the dashboard's SSE stream, carried by Postgres NOTIFY."""

import json
from typing import Any

import psycopg

CHANNEL = "ipo_events"


def emit(conn: psycopg.Connection, kind: str, **data: Any) -> None:
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, json.dumps({"kind": kind, **data})))

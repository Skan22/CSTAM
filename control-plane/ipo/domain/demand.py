"""How many warm VMs the pool should hold right now."""

import psycopg

from ipo.settings import Settings


def demand_target(conn: psycopg.Connection, base: int, settings: Settings) -> int:
    """The configured target, or as many VMs as teams registered in the recent window if that is
    more, but never past `pool_max`.

    Registrations come in waves (a session starts, a team signs up, a hundred follow), and a pool
    sized for the last wave is warm for the next one. When the window passes the target falls
    back and the pool manager discards the idle surplus, so the extra VMs cost only as long as
    the demand that justified them."""
    row = conn.execute(
        "SELECT count(*) FROM teams WHERE created_at > now() - make_interval(secs => %s)",
        (settings.demand_window_seconds,)).fetchone()
    recent = int(row[0]) if row else 0
    return max(base, min(settings.pool_max, recent))

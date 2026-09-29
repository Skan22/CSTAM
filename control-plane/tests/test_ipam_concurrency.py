"""M3 gate: many threads claiming addresses at once never produce a duplicate.

IPO_CONCURRENCY_RUNS sets the repeat count (the gate uses 1000; the default keeps CI quick).
IPO_BENCH_OUT, if set, receives the p95 claim time as JSON.
"""

import json
import os
import statistics
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg

from ipo.domain import leases

THREADS = 50
ADDRESSES = 100
RUNS = int(os.environ.get("IPO_CONCURRENCY_RUNS", "25"))


def _reset(conn: psycopg.Connection, pooled: int) -> list[str]:
    conn.execute("TRUNCATE leases, teams CASCADE")
    team_ids = [
        r[0] for r in conn.execute(
            "INSERT INTO teams (slug, subdomain, owner, expires_at)"
            " SELECT 't' || g, 't' || g || '.x', 'o', now() + interval '1 hour'"
            " FROM generate_series(1, %s) g RETURNING id::text", (THREADS,))
    ]
    leases.seed(conn, [f"10.20.{i // 250}.{i % 250 + 1}" for i in range(ADDRESSES)])
    conn.execute(
        "UPDATE leases SET state = 'pooled' WHERE ip IN"
        " (SELECT ip FROM leases ORDER BY ip LIMIT %s)", (pooled,))
    return team_ids


def _race(dsn: str, pooled: int) -> tuple[list[str | None], list[float], int]:
    admin = psycopg.connect(dsn, autocommit=True)
    workers = [psycopg.connect(dsn, autocommit=True) for _ in range(THREADS)]
    latencies: list[float] = []
    results: list[str | None] = [None] * THREADS
    try:
        return _run(admin, workers, pooled, latencies, results)
    finally:
        for c in [admin, *workers]:
            c.close()


def _run(
    admin: psycopg.Connection, workers: list[psycopg.Connection], pooled: int,
    latencies: list[float], results: list[str | None],
) -> tuple[list[str | None], list[float], int]:
    expires = datetime.now(UTC) + timedelta(hours=1)
    lock = threading.Lock()
    failures = 0
    for _ in range(RUNS):
        teams = _reset(admin, pooled)
        barrier = threading.Barrier(THREADS)

        def claim(i: int, barrier: threading.Barrier = barrier, teams: list[str] = teams) -> None:
            nonlocal failures
            barrier.wait()
            t0 = time.perf_counter()
            try:
                got = leases.claim_pooled(workers[i], team_id=teams[i], expires_at=expires)
            except psycopg.Error:
                with lock:
                    failures += 1
                return
            dt = time.perf_counter() - t0
            with lock:
                latencies.append(dt)
                results[i] = got.ip if got else None

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got_ips = [ip for ip in results if ip]
        assert len(got_ips) == len(set(got_ips)), "duplicate IP handed out"
        assert len(got_ips) == min(THREADS, pooled)
        row = admin.execute("SELECT count(*) FROM leases WHERE state = 'leased'").fetchone()
        assert row and row[0] == len(got_ips)
        results[:] = [None] * THREADS
    return results, latencies, failures


def test_50_threads_claim_100_addresses_without_duplicates(dsn: str) -> None:
    _, latencies, failures = _race(dsn, pooled=ADDRESSES)
    assert failures == 0
    p95 = statistics.quantiles(latencies, n=100)[94]
    print(f"\nclaim p95 = {p95 * 1000:.1f} ms over {len(latencies)} claims in {RUNS} runs")
    out = os.environ.get("IPO_BENCH_OUT")
    if out:
        with open(out, "w") as f:
            json.dump({"runs": RUNS, "claims": len(latencies), "p95_seconds": p95}, f)


def test_contended_pool_hands_each_address_out_exactly_once(dsn: str) -> None:
    """More claimers than pooled addresses: exactly `pooled` succeed, the rest get None."""
    _, _, failures = _race(dsn, pooled=30)
    assert failures == 0

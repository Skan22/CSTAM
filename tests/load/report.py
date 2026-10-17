"""Turns the measurements of a benchmark run into the Markdown tables of the report.

    python -m load.report RUN_DIR [--real real-cloud.jsonl] > benchmarks-tables.md

RUN_DIR holds `load-report.jsonl` and `chaos-report.jsonl` (found anywhere below it). The
optional real-cloud file has the same line format, from `python -m load.real`.
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def read(root: Path, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob(name)):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def table(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def stat(row: dict[str, Any], key: str, field: str) -> float:
    return float(row.get(key, {}).get(field, float("nan")))


def provisioning(rows: list[dict[str, Any]]) -> str:
    out = []
    for name, label in (("bench-warm", "Warm (VM already booted, one team at a time)"),
                        ("bench-cold", "Cold (address reserved and VM created on demand)")):
        r = next((x for x in rows if x["scenario"] == name), None)
        if not r:
            continue
        out.append([label, str(r["count"]), f"{r['ok']}/{r['size']}"] + [
            f"{stat(r, k, f):.2f}" for k, f in (("accept", "p50"), ("active", "p50"),
                                                ("routed", "p50"), ("routed", "p95"),
                                                ("routed", "max"))])
    return table(["Path", "Teams", "OK", "API accepts p50 (s)", "Active p50 (s)",
                  "Serving p50 (s)", "Serving p95 (s)", "Serving max (s)"], out)


def bursts(rows: list[dict[str, Any]]) -> str:
    by: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["scenario"].startswith("burst-") and r["scenario"][6:].isdigit():
            by[int(r["scenario"][6:])].append(r)
    out = []
    for size in sorted(by):
        rs = by[size]
        walls = [r["wall_s"] for r in rs]
        routed_p50 = [stat(r, "routed", "p50") for r in rs]
        routed_max = [stat(r, "routed", "max") for r in rs]
        out.append([str(size), str(len(rs)), f"{sum(r['ok'] for r in rs)}/{sum(r['size'] for r in rs)}",
                    str(sum(len(r["duplicate_ips"]) for r in rs)),
                    f"{statistics.median(routed_p50):.2f}", f"{statistics.median(walls):.2f}",
                    f"{max(walls):.2f}", f"{max(routed_max):.2f}"])
    return table(["Simultaneous teams", "Rounds", "Succeeded", "Duplicate addresses",
                  "Median team served (s)", "Whole burst served, median (s)",
                  "Whole burst, worst (s)", "Slowest single team (s)"], out)


def over_pool(rows: list[dict[str, Any]]) -> str:
    r = next((x for x in rows if x["scenario"] == "burst-12-over-pool-3"), None)
    if not r:
        return ""
    return (f"A burst of {r['size']} teams against a warm pool of {r['pool_target']}: "
            f"{r['paths']['warm']} warm and {r['paths']['cold']} cold claims, "
            f"{r['ok']}/{r['size']} succeeded, whole burst served in {r['wall_s']:.2f} s.\n")


def failover(rows: list[dict[str, Any]]) -> str:
    groups = {
        "Primary gateway VM dies (power off)": [r for r in rows if r["scenario"].startswith("vm-death-")],
        "Traefik crashes on the primary (kill -9)": [r for r in rows
                                                   if r["scenario"].startswith("traefik-crash-")],
        "keepalived stopped cleanly": [r for r in rows if r["scenario"] == "keepalived-stop"],
        "Manual failover through the agent": [r for r in rows if r["scenario"] == "agent-fault"],
    }
    out = []
    for label, rs in groups.items():
        if rs:
            gaps = [r["worst_gap_s"] for r in rs]
            out.append([label, str(len(rs)), str(sum(r["failed"] for r in rs)),
                        f"{min(gaps):.2f}", f"{statistics.median(gaps):.2f}", f"{max(gaps):.2f}"])
    return table(["Failure", "Runs", "Requests lost in the gaps", "Shortest gap (s)", "Median gap (s)",
                  "Longest gap (s)"], out)


def simple(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [r for r in rows if r["scenario"] == name]


def render(root: Path, real: Path | None) -> str:
    load, chaos = read(root, "load-report.jsonl"), read(root, "chaos-report.jsonl")
    out = ["## Lab measurements\n"]
    out += ["### Provisioning latency\n", provisioning(load)]
    out += ["### Simultaneous registrations\n", bursts(load), over_pool(load)]
    out += ["### Gateway failover under 50 requests/s\n", failover(chaos)]
    rb = simple(load, "bench-rollback")
    if rb:
        r = rb[0]
        out += [f"### Config rollback\n\n{r['count']} rollbacks: p50 {r['p50_s']:.3f} s, "
                f"p95 {r['p95_s']:.3f} s, max {r['max_s']:.3f} s until both gateways run it.\n"]
    life = simple(load, "lifecycle-expiry")
    if life:
        r = life[0]
        out += ["### Expiry and IP recycling\n", table(
            ["Teams", "Quarantine (s)", "Route gone (s)", "DNS gone (s)", "Team deleted (s)",
             "Address free (s)", "Recycle overhead p50 / max (s)"],
            [[str(r["teams"]), str(r["quarantine_s"]), f"{r['route_s']:.2f}", f"{r['dns_s']:.2f}",
              f"{r['deleted_s']:.2f}", f"{r['freed_s']:.2f}",
              f"{r['recycle_overhead_p50_s']:.2f} / {r['recycle_overhead_max_s']:.2f}"]])]
    tp = sorted((r for r in load if r["scenario"].startswith("bench-throughput-")),
                key=lambda r: r["target_rps"])
    if tp:
        out += ["### Gateway throughput across 30 teams\n", table(
            ["Offered (req/s)", "Achieved (req/s)", "Failed", "p50 (ms)", "p95 (ms)", "p99 (ms)"],
            [[str(r["target_rps"]), f"{r['achieved_rps']:.0f}", f"{r['failed']}/{r['requests']}",
              f"{r['p50_ms']:.1f}", f"{r['p95_ms']:.1f}", f"{r['p99_ms']:.1f}"] for r in tp])]
    cp = simple(load, "bench-cp-crash")
    if cp:
        out += ["### Control plane killed under load\n", table(
            ["Run", "Down for (s)", "Requests", "Failed", "Restart to serving (s)", "p99 (ms)"],
            [[str(r["run"] + 1), str(r["outage_s"]), str(r["requests"]), str(r["failed"]),
              f"{r['recovery_s']:.2f}", f"{r['p99_ms']:.1f}"] for r in cp])]
    if real and real.exists():
        out += ["## Real FelCloud measurements\n", render_real(read(real.parent, real.name))]
    return "\n".join(out)


def render_real(rows: list[dict[str, Any]]) -> str:
    out = []
    for r in rows:
        out.append([r["scenario"], json.dumps({k: v for k, v in r.items() if k != "scenario"})])
    return table(["Scenario", "Result"], out)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--real", type=Path)
    a = p.parse_args()
    sys.stdout.write(render(a.run_dir, a.real))


if __name__ == "__main__":
    main()

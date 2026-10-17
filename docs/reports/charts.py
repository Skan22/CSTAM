"""Draws the benchmark report's charts from a run's measurements.

    python docs/reports/charts.py RUN_DIR [real-cloud.jsonl ...]

RUN_DIR holds `load-report.jsonl` and `chaos-report.jsonl` (found anywhere below it); the other
arguments are `bench.real_cloud` outputs. Writes PNGs next to this script. Needs matplotlib.
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).parent
INK, MUTED, GRID = "#2b2540", "#6b6584", "#e6e2f0"
PURPLE, TEAL, AMBER, CORAL = "#7a4fd6", "#1f9e8f", "#e0a030", "#d6504f"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK,
    "xtick.color": INK, "ytick.color": INK, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "axes.axisbelow": True, "figure.dpi": 150,
})


def read(root: Path, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob(name)):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def bursts(load: list[dict[str, Any]]) -> None:
    by: dict[int, list[float]] = defaultdict(list)
    for r in load:
        if r["scenario"].startswith("burst-") and r["scenario"][6:].isdigit():
            by[int(r["scenario"][6:])].append(r["wall_s"])
    sizes = sorted(by)
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    med = [statistics.median(by[s]) for s in sizes]
    worst = [max(by[s]) for s in sizes]
    ax.fill_between(sizes, med, worst, color=PURPLE, alpha=0.15, label="worst of 5 rounds")
    ax.plot(sizes, med, "o-", color=PURPLE, lw=2.2, label="median of 5 rounds")
    for s, m in zip(sizes, med, strict=True):
        ax.annotate(f"{m:.1f} s", (s, m), textcoords="offset points", xytext=(0, 9), ha="center",
                    color=INK, fontsize=10)
    ax.set_xlabel("teams registering at the same instant")
    ax.set_ylabel("until every team is serving traffic (s)")
    ax.set_xticks(sizes)
    ax.set_ylim(0, max(worst) * 1.25)
    ax.set_title("Simultaneous registrations (lab, real gateways)", loc="left", color=INK)
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(HERE / "chart-bursts.png")
    plt.close(fig)


def failover(chaos: list[dict[str, Any]]) -> None:
    groups = [("primary VM dies", "vm-death-", TEAL), ("Traefik crashes", "traefik-crash-", AMBER)]
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    for i, (label, prefix, colour) in enumerate(groups):
        gaps = [r["worst_gap_s"] for r in chaos if r["scenario"].startswith(prefix)]
        ax.boxplot(gaps, positions=[i], vert=False, widths=0.5, patch_artist=True,
                   boxprops={"facecolor": colour, "alpha": 0.35, "edgecolor": colour},
                   medianprops={"color": INK, "lw": 2}, whiskerprops={"color": colour},
                   capprops={"color": colour}, flierprops={"markeredgecolor": colour})
    ax.axvline(2, color=CORAL, ls="--", lw=1.2)
    ax.axvline(4, color=CORAL, ls="--", lw=1.2)
    ax.text(2.03, 1.45, "target 2 s", color=CORAL, fontsize=9)
    ax.text(4.03, 1.45, "target 4 s", color=CORAL, fontsize=9)
    ax.set_yticks(range(len(groups)), [g[0] for g in groups])
    ax.set_xlim(0, 4.6)
    ax.set_xlabel("longest stretch with no successful request (s), 10 runs each")
    ax.set_title("Gateway failover under 50 requests/s", loc="left", color=INK)
    fig.tight_layout()
    fig.savefig(HERE / "chart-failover.png")
    plt.close(fig)


def real_cloud(files: list[Path]) -> None:
    rows: list[dict[str, Any]] = []
    for f in files:
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    warm = [r for r in rows if r["scenario"] == "real-burst-warm"]
    cold = [r for r in rows if r["scenario"] == "real-burst-cold"]
    if not warm or not cold:
        return
    labels, values, colours = [], [], []
    for r in sorted(warm, key=lambda r: r["size"]):
        labels.append(f"{r['size']} teams, warm pool")
        values.append(r["wall_s"])
        colours.append(TEAL)
    labels.append("10 teams, cold (Nova boots)")
    values.append(statistics.median(r["wall_s"] for r in cold))
    colours.append(AMBER)
    fig, ax = plt.subplots(figsize=(7.5, 3.0))
    ax.barh(labels[::-1], values[::-1], color=colours[::-1])
    ax.set_xscale("log")
    ax.set_xlabel("until every team is active, on FelCloud (s, log scale)")
    for y, v in enumerate(values[::-1]):
        ax.text(v * 1.08, y, f"{v:.1f} s", va="center", color=INK, fontsize=10)
    ax.set_xlim(0.8, max(values) * 3)
    ax.set_title("Registration on the real cloud", loc="left", color=INK)
    fig.tight_layout()
    fig.savefig(HERE / "chart-real-cloud.png")
    plt.close(fig)


def main() -> None:
    root = Path(sys.argv[1])
    load, chaos = read(root, "load-report.jsonl"), read(root, "chaos-report.jsonl")
    bursts(load)
    failover(chaos)
    real_cloud([Path(p) for p in sys.argv[2:]])


if __name__ == "__main__":
    main()

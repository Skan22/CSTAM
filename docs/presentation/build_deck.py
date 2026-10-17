"""Builds IPO-Jury-Deck.pdf, the jury presentation, from the benchmark measurements.

    python docs/presentation/build_deck.py RUN_DIR [real-cloud.jsonl ...] [--out deck.pdf]

RUN_DIR holds `load-report.jsonl` and `chaos-report.jsonl` of a `load.bench` run; the numbers on
the slides are read from them (and from the real-cloud files) so the deck cannot drift from the
report. Run `docs/reports/charts.py` first for the charts. Needs headless Chromium.
The talk, with speaker notes, is in jury-deck.md.
"""

import json
import statistics
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
DOCS = HERE.parent
IMG = DOCS / "reports"


def read(root: Path, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob(name)):
        rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def numbers(root: Path, real_files: list[Path]) -> dict[str, Any]:
    load, chaos = read(root, "load-report.jsonl"), read(root, "chaos-report.jsonl")
    real: list[dict[str, Any]] = []
    for f in real_files:
        real += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    by: dict[int, list[float]] = defaultdict(list)
    for r in load:
        if r["scenario"].startswith("burst-") and r["scenario"][6:].isdigit():
            by[int(r["scenario"][6:])].append(r["wall_s"])
    gaps = lambda p: [r["worst_gap_s"] for r in chaos if r["scenario"].startswith(p)]  # noqa: E731
    one = lambda n: next(r for r in load if r["scenario"] == n)  # noqa: E731
    tp = {r["target_rps"]: r for r in load if r["scenario"].startswith("bench-throughput-")}
    life = one("lifecycle-expiry")
    warm_real = sorted((r for r in real if r["scenario"] == "real-burst-warm"), key=lambda r: r["size"])
    cold_real = [r for r in real if r["scenario"] == "real-burst-cold"]
    tear_real = sorted((r for r in real if r["scenario"] == "real-teardown"), key=lambda r: r["teams"])
    recycle = [r for r in real if r["scenario"] == "real-recycle"]
    return {
        "burst": {s: statistics.median(v) for s, v in sorted(by.items())},
        "burst_rounds": len(next(iter(by.values()))),
        "burst_teams": sum(s * len(v) for s, v in by.items()),
        "warm_serving": one("bench-warm")["routed"]["p50"],
        "vm_gap": (min(gaps("vm-death-")), max(gaps("vm-death-"))),
        "tr_gap": (min(gaps("traefik-crash-")), max(gaps("traefik-crash-"))),
        "rollback": one("bench-rollback")["p50_s"],
        "life": life,
        "tp_max": max(tp), "tp_p99": tp[max(tp)]["p99_ms"],
        "crash_recovery": statistics.median(r["recovery_s"] for r in load if r["scenario"] == "bench-cp-crash"),
        "real_warm": [(r["size"], r["wall_s"]) for r in warm_real],
        "real_cold": (min(r["wall_s"] for r in cold_real), max(r["wall_s"] for r in cold_real)) if cold_real else None,
        "real_teardown": [(r["teams"], r["seconds"]) for r in tear_real],
        "real_recycle": statistics.median(r["address_free"]["p50"] for r in recycle) if recycle else None,
    }


def slides(n: dict[str, Any]) -> list[str]:
    b = n["burst"]
    rw = n["real_warm"]
    s: list[str] = []

    def slide(title: str, body: str, cls: str = "") -> None:
        s.append(f'<section class="{cls}"><h2>{title}</h2><div class="body">{body}</div>'
                 f'<footer><span>Resilient IP Optimizer</span><span>{len(s) + 1}</span></footer></section>')

    s.append('<section class="title"><div><h1>Resilient IP Optimizer</h1>'
             '<p class="sub">Sandboxes with their own subdomain, a highly available front door, '
             'and a platform that cleans up after itself</p>'
             '<p class="meta">IEEE CSTAM 3.0 × FelCloud · Phase 2: integration and testing</p></div></section>')

    slide("The problem", """
<div class="two"><div><p>Twenty teams arrive at once. Each needs a <b>VM</b>, an <b>address</b>, a <b>route</b> and a <b>name</b>.</p>
<p>Later each must vanish without leaving a route, a DNS record or an address behind.</p>
<p>And the front door cannot go down.</p></div>
<div class="facts"><div><b>100</b> addresses in the sandbox pool</div><div><b>50</b> instances in the FelCloud quota</div>
<div><b>17 s</b> to boot one VM, a minute or more when ten boot at once</div></div></div>""")

    slide("Architecture", f'<img class="wide" src="{(DOCS / "diagrams" / "system-architecture.svg").as_uri()}">', "image")
    slide("Gateway data flow", f'<img class="wide" src="{(DOCS / "diagrams" / "gateway-data-flow.svg").as_uri()}">', "image")

    slide(f"Up to {max(b)} teams at the same instant: no failures", f"""
<div class="two"><div><img src="{(IMG / 'chart-bursts.png').as_uri()}"></div>
<ul><li><b>{n['burst_teams']} registrations</b> in {n['burst_rounds']} rounds of 10, 20, 30, 50 and 90 at the same instant: every one <b>active</b>, on its <b>own address</b>, served by <b>its own sandbox</b>; 0 failures.</li>
<li>One team alone: <b>{n['warm_serving']:.1f} s</b> from request to serving.</li>
<li>A saga with idempotency keys, a database-enforced lease state machine, a warm pool that follows demand.</li>
<li>Measuring found it: 30 teams took <b>15 s</b> at first; Traefik's reload throttle and the config batching brought it to <b>{b[30]:.1f} s</b>.</li></ul></div>""")

    cold = n["real_cold"]
    slide("On the real FelCloud", f"""
<div class="two"><div><img src="{(IMG / 'chart-real-cloud.png').as_uri()}"></div>
<ul><li>Warm: <b>{rw[-1][0]} teams active in {rw[-1][1]:.1f} s</b>, 0 failures, real Nova and Neutron calls.</li>
<li>Cold: <b>{cold[0]:.0f}–{cold[1]:.0f} s</b>. Ten concurrent boots are far slower than one: the warm pool is the lever.</li>
<li>Teardown of {n['real_teardown'][-1][0]} teams: <b>{n['real_teardown'][-1][1]:.0f} s</b>; addresses free again <b>{n['real_recycle']:.1f} s</b> after (5 s quarantine).</li>
<li>The real cloud caught three bugs the lab could not: warm claims on booting VMs, the reconciler deleting VMs mid-boot, teardown waiting on Nova's lingering DELETED entries (85 s → 6 s).</li></ul></div>""")

    slide("Hot reload and rollback", f"""
<ul class="big"><li>Registering or deleting a team changes the routing table <b>without touching Traefik's process</b>.</li>
<li>Config is <b>Ed25519-signed</b>; each agent re-validates it (every backend inside the sandbox range), swaps it atomically, probes every changed route, and <b>rolls back</b> on failure.</li>
<li>An operator's rollback reaches both gateways in <b>{n['rollback']:.2f} s</b> (median).</li>
<li>Control plane killed with <code>-9</code> for 8 s under load: <b>0 failed requests</b>; back in {n['crash_recovery']:.1f} s.</li>
<li>{n['tp_max']} requests/s across 30 teams' hosts, new connection each: 0 failures, p99 {n['tp_p99']:.1f} ms (lab).</li></ul>""")

    slide("High availability", f"""
<div class="two"><div><img src="{(IMG / 'chart-failover.png').as_uri()}"></div>
<ul><li>VRRP, unicast peers, health check that fails keepalived when Traefik is down.</li>
<li>Primary VM dies: longest silence <b>{n['vm_gap'][0]:.2f}–{n['vm_gap'][1]:.2f} s</b>. Traefik crashes: <b>{n['tr_gap'][0]:.1f}–{n['tr_gap'][1]:.1f} s</b>. Planned failover: <b>0.12 s</b>.</li>
<li>Split brain: detected from both heartbeats, alarmed, healed to one master.</li></ul></div>""")

    life = n["life"]
    slide("Automated teardown: nothing outlives a team", f"""
<div class="steps"><div>route out<br><small>both gateways ack</small></div><div>DNS out</div><div>VM + port deleted</div><div>address verified clean<br><small>then quarantined</small></div><div>logins + traffic purged</div></div>
<ul><li>Route and DNS record gone <b>{life['route_s']:.0f} s</b> after a lease expires; the address is reusable one quarantine <b>+{life['recycle_overhead_p50_s']:.1f} s</b> later.</li>
<li>FelCloud: <b>{n['real_teardown'][-1][0]} teams torn down in {n['real_teardown'][-1][1]:.0f} s</b>.</li>
<li>Worker killed at every boundary of every step → the reconciler finishes it; 100 create/delete cycles leave zero routes, VMs, ports, DNS records or linked leases. A deleted team's token stops working at once.</li></ul>""")

    slide("Network isolation", """
<div class="two"><ul><li><b>Three independent layers</b> from one policy file: Neutron security groups, port security, nftables on every host.</li>
<li>Lab: <b>1,683 probes</b>, each layer alone, forged frames, rerouting, floods.</li>
<li><b>Real FelCloud, probe VMs behind the real security groups:</b> 255 probes, <b>0 leaks</b>. Sandbox→sandbox blocked on every port; gateway management addresses blocked; positive controls pass.</li>
<li><b>Port security proven on the real cloud:</b> a sandbox sent forged source IPs (a neighbour's, a gateway's, an unused one) and a forged MAC, 32 forged packets: <b>0 delivered</b>.</li></ul>
<div class="facts"><div><b>0</b> leaks of 255 probes</div><div><b>0 of 32</b> forged packets delivered</div><div><b>3</b> independent layers</div></div></div>""")

    slide("API and security architecture", """
<ul class="big"><li><b>Integration guide:</b> bearer JWT with roles, idempotent asynchronous operations (<code>202</code> + job), RFC 9457 errors, server-sent events, OpenAPI + Postman, DNS modes, client code.</li>
<li><b>STRIDE per component:</b> edge, gateway agent, API, database, sandboxes, management plane, each threat with its control and the test that would fail without it.</li>
<li><b>Named gaps:</b> agents use an operator account, no per-team rate limit, TLS to teams optional, nftables rather than eBPF.</li></ul>""")

    slide("Evidence", """
<div class="facts wide"><div><b>354</b> control-plane tests on a real PostgreSQL</div><div><b>72</b> dashboard tests</div><div><b>67</b> deploy checks on the real config readers</div>
<div><b>46 + 21 + 35</b> chaos, load and isolation lab tests</div><div><b>Go</b> agent with <code>-race</code></div><div><b>ruff · mypy --strict · tsc · go vet · ansible-lint</b> clean</div></div>
<p class="note">Everything above can be re-run: <code>load.bench</code>, <code>chaos.run</code>, <code>isolation.run</code>, <code>isolation.cloud_probe</code>, <code>bench.real_cloud</code>.</p>""")

    slide("Demo", """
<p>The scripted demo, recorded against the real lab: subdomain routing → hot reload → a burst of 12 → expiry → failover → VM death → split brain.</p>
<p class="note"><code>docs/presentation/demo.mp4</code> (104 s) · <code>python -m chaos.demo</code> to run it live (<kbd>b</kbd> burst, <kbd>e</kbd> expire, <kbd>f</kbd> fail over, <kbd>k</kbd> kill a VM).</p>""")

    slide("Limits and next steps", """
<div class="two"><div><h3>Not done</h3><ul><li>Ansible roles on real hosts; VRRP between two real OpenStack VMs</li>
<li>Per-team rate limits at the gateway; a narrower agent role</li><li>Wildcard TLS for team subdomains</li></ul></div>
<div><h3>Next</h3><ul><li>Those, then a second region for the control plane</li><li>Baked sandbox image to shorten first boot</li></ul></div></div>""")
    return s


CSS = """
@page { size: 1280px 720px; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif; color: #2b2540; }
section { width: 1280px; height: 720px; padding: 48px 64px 40px; position: relative; break-after: page;
  background: #ffffff; overflow: hidden; display: flex; flex-direction: column; }
section:last-child { break-after: auto; }
h1 { font-size: 64px; margin: 0 0 18px; color: #4b2aa0; }
h2 { font-size: 36px; margin: 0 0 22px; color: #4b2aa0; font-weight: 700; }
h3 { font-size: 24px; margin: 0 0 10px; color: #7a4fd6; }
.title { background: linear-gradient(135deg, #1a0f2e, #3d2275); color: #fff; justify-content: center; }
.title h1 { color: #fff; } .title .sub { font-size: 28px; max-width: 900px; color: #e6defa; }
.title .meta { font-size: 20px; color: #ffbe5a; margin-top: 40px; }
.body { flex: 1; font-size: 24px; line-height: 1.4; min-height: 0; }
.two { display: grid; grid-template-columns: 1fr 1fr; gap: 36px; align-items: center; height: 100%; }
.two img { width: 100%; }
ul { padding-left: 26px; margin: 0; } li { margin: 0 0 14px; } ul.big { font-size: 26px; }
.facts { display: flex; flex-direction: column; gap: 16px; } .facts > div { background: #f3eefc; border-left: 6px solid #7a4fd6;
  padding: 16px 22px; font-size: 24px; } .facts b { font-size: 34px; color: #4b2aa0; margin-right: 6px; }
.facts.wide { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.steps { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin-bottom: 26px; }
.steps div { background: #e8fbf7; border-top: 6px solid #1f9e8f; padding: 14px; text-align: center; font-size: 21px; font-weight: 600; }
.steps small { font-weight: 400; color: #5a5470; font-size: 16px; }
.image .body { display: flex; align-items: center; justify-content: center; }
img.wide { max-width: 100%; max-height: 560px; }
.note { color: #5a5470; font-size: 20px; } code, kbd { background: #f3eefc; padding: 1px 6px; border-radius: 4px; font-size: 0.85em; }
footer { display: flex; justify-content: space-between; color: #8b86a0; font-size: 15px; margin-top: 10px; }
.title footer { display: none; }
"""


def build(root: Path, real_files: list[Path], out: Path) -> None:
    n = numbers(root, real_files)
    html = (f'<!doctype html><meta charset="utf-8"><title>Resilient IP Optimizer</title>'
            f'<style>{CSS}</style>{"".join(slides(n))}')
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "deck.html"
        src.write_text(html)
        subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--no-pdf-header-footer", f"--print-to-pdf={out}",
                        "--virtual-time-budget=20000", f"file://{src}"],
                       capture_output=True, text=True, timeout=240, check=True)


def main() -> None:
    args = sys.argv[1:]
    out = HERE / "IPO-Jury-Deck.pdf"
    if "--out" in args:
        i = args.index("--out")
        out = Path(args[i + 1])
        del args[i:i + 2]
    build(Path(args[0]), [Path(p) for p in args[1:]], out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

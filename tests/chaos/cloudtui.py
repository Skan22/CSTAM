"""The screen for `pulumi up` and `pulumi destroy` against the real cloud.

`render(dash, size, now)` is pure, so it is tested and screenshotted without a cloud. `run()` owns
the keyboard, starts Pulumi (chaos/cloud.py), keeps asking the OpenStack APIs what exists, and
returns what was built. The one authored motion is the creation wave: a resource that finishes
flashes for a moment, its component's bar fills, and the real cloud's counters catch up.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import threading
import time
import traceback
import tty
from dataclasses import dataclass, field
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos import theme as t
from chaos.cloud import CloudConfig, Probe, Pulumi, Res, Run, Server, Snapshot

STAGES = ["build", "operate", "break", "teardown"]
KIND = {"Network": "net", "Subnet": "subnet", "Router": "router", "RouterInterface": "iface",
        "SecGroup": "group", "SecGroupRule": "rule", "ServerGroup": "affinity", "Port": "port",
        "Instance": "vm", "FloatingIp": "fip", "FloatingIpAssociate": "fip-link", "Keypair": "key"}
# What the real cloud should hold when everything is built, by the probe's counter names.
EXPECTED_KINDS = {"networks": "Network", "routers": "Router", "ports": "Port",
                  "security groups": "SecGroup", "servers": "Instance", "floating ips": "FloatingIp"}
COMPONENTS = ["Networks", "SecurityGroups", "GatewayPair", "ControlPlane", "Relay", "Bastion", ""]
PHASE_TEXT = {"plan": "planning", "ready": "ready", "run": "", "done": "", "failed": ""}


@dataclass
class CloudDash:
    verb: str = "up"  # up or destroy
    phase: str = "plan"  # plan, ready (waiting for Enter), run, done, failed
    project: str = ""
    region: str = ""
    run: Run = field(default_factory=lambda: Run("up"))
    plan: dict[str, int] = field(default_factory=dict)  # component -> resources to create
    planned: int = 0
    snap: Snapshot | None = None
    notes: list[tuple[str, str]] = field(default_factory=list)  # (level, text)
    floating_ips: bool = True
    hosts: dict[str, dict[str, str]] = field(default_factory=dict)  # platform.yaml's network.hosts
    quit: bool = False
    t0: float = field(default_factory=t.now)
    t_done: float = 0.0

    def note(self, level: str, text: str) -> None:
        self.notes.append((level, text))

    @property
    def total(self) -> int:
        return max(self.planned, len(self.run.resources))

    @property
    def done(self) -> int:
        return sum(1 for r in self.run.resources.values() if r.status == "done")


def comp_rows(d: CloudDash) -> list[tuple[str, int, int, int]]:
    """(component, done, working, total) in build order, from the plan plus what has arrived."""
    seen: dict[str, list[Res]] = {}
    for r in d.run.resources.values():
        seen.setdefault(r.parent, []).append(r)
    rows = []
    for name in COMPONENTS:
        items = seen.get(name, [])
        total = max(d.plan.get(name, 0), len(items))
        if total:
            rows.append((name or "Project", sum(r.status == "done" for r in items),
                         sum(r.status == "working" for r in items), total))
    return rows


def spinner_or_tick(done: int, working: int, total: int, now: float) -> Text:
    if done == total:
        return Text("✔", style=f"bold {t.MINT}")
    return Text(t.spinner(now), style=f"bold {t.AMBER}") if working else Text("·", style=t.DIM)


def building_panel(d: CloudDash, width: int, height: int, now: float) -> Panel:
    verb = "pulumi up" if d.verb == "up" else "pulumi destroy"
    inner = max(30, width - 4)
    bar_w = max(10, inner - 15 - 9 - 2 - 3)
    comps = Table.grid(padding=(0, 1))
    comps.add_column(width=15, no_wrap=True)
    comps.add_column(width=bar_w)
    comps.add_column(width=8, justify="right", no_wrap=True)
    comps.add_column(width=2)
    components = comp_rows(d)
    for name, n_done, n_work, total in components:
        colour = t.AMBER if n_work else t.MINT
        comps.add_row(Text(name, style=t.TEXT if n_work or n_done < total else t.DIM),
                      t.bar(n_done / total, bar_w, colour), Text(f"{n_done}/{total}", style=t.TEXT),
                      spinner_or_tick(n_done, n_work, total, now))
    feed = Table.grid(padding=(0, 1))
    feed.add_column(width=2, no_wrap=True)
    feed.add_column(width=8, no_wrap=True)
    feed.add_column(width=8, no_wrap=True)
    feed.add_column(width=max(10, inner - 2 - 8 - 8 - 7 - 4), no_wrap=True, overflow="ellipsis")
    feed.add_column(width=6, justify="right", no_wrap=True)
    working = [r for r in d.run.resources.values() if r.status == "working"][-6:]
    for r in working:
        feed.add_row(Text(t.spinner(now), style=f"bold {t.AMBER}"),
                     Text("creating" if d.verb == "up" else "deleting", style=t.AMBER),
                     Text(KIND.get(r.kind, r.kind), style=t.DIM), Text(r.name, style=f"bold {t.TEXT}"),
                     Text(f"{now - r.started:.1f}s", style=t.DIM))
    room = max(2, height - 2 - len(components) - 2 - len(working) - 1)
    finished = [r for r in d.run.resources.values() if r.status in ("done", "failed")][-room:]
    gap = Table.grid()
    gap.add_row(Text(""))
    for r in finished:
        if r.status == "failed":
            feed.add_row(Text("✖", style=f"bold {t.CORAL}"), Text("failed", style=t.CORAL),
                         Text(KIND.get(r.kind, r.kind), style=t.CORAL), Text(r.name, style=t.CORAL), "")
            continue
        fresh = now - r.ended < 0.5  # the wave: a finished resource flashes, then settles
        feed.add_row(Text("✔", style=f"bold #120a1a on {t.MINT}" if fresh else f"bold {t.MINT}"),
                     Text("created" if d.verb == "up" else "deleted",
                          style=f"bold {t.MINT}" if fresh else t.DIM),
                     Text(KIND.get(r.kind, r.kind), style=t.DIM),
                     Text(r.name, style=t.TEXT if fresh else t.DIM),
                     Text(f"{r.ended - r.started:.1f}s", style=t.DIM))
    title = f"{verb} · {d.done} of {d.total}" if d.total else verb
    sub = t.clock(d.run.elapsed) if d.run.started else ""
    return t.panel(Group(comps, Text(""), feed), title, border=t.AMBER if d.phase == "run" else t.EDGE,
                   subtitle=sub, heavy=d.phase == "run")


def planned_count(d: CloudDash, kind: str) -> int:
    return sum(1 for r in d.run.resources.values() if r.kind == kind)


LANES = [("edge", "edge-net", "r-edge"), ("sandbox", "sandbox-net", "r-edge"),
         ("mgmt", "mgmt-net", "r-mgmt")]  # (platform.yaml key, network, router)
VM_ORDER = ["gw-a", "gw-b", "cp-1", "cp-2", "relay", "bastion"]


def vm_glyph(status: str, now: float) -> tuple[str, str]:
    return {"ACTIVE": ("●", t.MINT), "BUILD": (t.spinner(now), t.AMBER), "ERROR": ("✖", t.CORAL)}.get(
        status, ("○", t.DIM))


def network_map(d: CloudDash, now: float) -> Table:
    """Three networks, the routers that join them to the internet, and every VM in the lanes it is
    plugged into; each part lights up as the cloud reports it."""
    s = d.snap
    names = s.networks if s else set()
    status = {sv.name: sv.status for sv in s.servers} if s else {}
    hosts = d.hosts or {}
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=8, no_wrap=True)
    grid.add_column(width=6, no_wrap=True)
    grid.add_column(no_wrap=True, overflow="ellipsis")
    for key, net, router in LANES:
        up = net in names
        r_up = router in names
        nodes = Text()
        for vm in VM_ORDER:
            if key in hosts.get(vm, {}):
                glyph, colour = vm_glyph(status.get(vm, ""), now)
                nodes.append(f"{glyph} ", style=f"bold {colour}")
                nodes.append(f"{vm} ", style=t.TEXT if status.get(vm) == "ACTIVE" else t.DIM)
        grid.add_row(Text(key, style=f"bold {t.MINT}" if up else t.DIM),
                     Text(router, style=t.MINT if r_up else t.DIM), nodes)
    return grid


def cloud_panel(d: CloudDash, width: int, now: float) -> Panel:
    s = d.snap
    head = Text.assemble((f" {d.region} ", f"bold #120a1a on {t.TEAL}"), (f"  {d.project}", t.DIM))
    counts = Table.grid(padding=(0, 1))
    for _ in range(2):
        counts.add_column(width=15)
        counts.add_column(width=4, justify="right")
        counts.add_column(width=2)
    cells: list[Text] = []
    for label, kind in EXPECTED_KINDS.items():
        planned = planned_count(d, kind)
        real = s.counts.get(label, 0) if s else 0
        if label == "floating ips" and not d.floating_ips:
            cells += [Text(label, style=t.DIM), Text("0", style=t.DIM), Text("✖", style=t.CORAL)]
            continue
        ok = (real >= planned > 0) if d.verb == "up" else real == 0
        cells += [Text(label, style=t.TEXT), Text(str(real), style=f"bold {t.TEXT}"),
                  Text("✔" if ok else "", style=f"bold {t.MINT}")]
    for i in range(0, len(cells), 6):
        counts.add_row(*cells[i:i + 6])
    servers = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    servers.add_column("vm", style=t.TEXT, no_wrap=True, min_width=8)
    servers.add_column("status", no_wrap=True, min_width=8)
    servers.add_column("size", style=t.DIM, no_wrap=True, min_width=5)
    servers.add_column("addresses", style=t.TEXT, no_wrap=True, overflow="ellipsis")
    for sv in (s.servers if s else []):
        glyph, colour = vm_glyph(sv.status, now)
        servers.add_row(sv.name, Text(f"{glyph} {sv.status}", style=f"bold {colour}"),
                        sv.flavor.rsplit(".", 1)[-1], " ".join(sv.addresses))
    if not (s and s.servers):
        servers.add_row(Text("no virtual machines yet", style=t.DIM), "", "", "")
    quota = Table.grid(padding=(0, 1))
    quota.add_column(width=10)
    quota.add_column(width=14)
    quota.add_column()
    for name, (used, limit) in (s.quota.items() if s else []):
        quota.add_row(Text(name, style=t.DIM), t.bar(used / limit if limit else 0, 14, t.AMBER),
                      Text(f"{used} / {limit}", style=t.TEXT))
    body = Group(head, Text(""), network_map(d, now), Text(""), counts, Text(""), servers, Text(""),
                 Text("your project's quota", style=t.DIM), quota)
    sub = "live from the OpenStack APIs" + (f" · {s.error}" if s and s.error else "")
    return t.panel(body, "in the real cloud", border=t.TEAL, subtitle=sub)


def log_panel(d: CloudDash, lines: int) -> Panel:
    out = Text()
    items: list[tuple[str, str]] = list(d.notes)
    if d.run.policy_resources:
        items.append(("ok" if not d.run.violations else "bad",
                      f"CrossGuard ipo-guardrails: {d.run.policy_checks} checks passed on "
                      f"{d.run.policy_resources} resources · {len(d.run.violations)} violations"))
    for v in d.run.violations:
        items.append(("bad", f"policy violation: {v}"))
    for e in d.run.errors[:3]:
        items.append(("bad", e))
    colours = {"info": t.TEXT, "ok": t.MINT, "warn": t.AMBER, "bad": t.CORAL, "dim": t.DIM}
    shown = items[-lines:]
    for i, (level, text) in enumerate(shown):
        out.append(text[:300], style=colours.get(level, t.TEXT))
        if i < len(shown) - 1:
            out.append("\n")
    return t.panel(out, "guardrails and notes")


def render(d: CloudDash, size: tuple[int, int], now: float | None = None, *,
           frozen: bool = False) -> Layout:
    """`frozen` draws a finished build as a record (the failover scene's view of the cloud)."""
    width, height = size
    now = t.now() if now is None else now
    root = Layout()
    bottom = 6
    middle = max(10, height - 3 - bottom - 3)
    root.split_column(Layout(name="head", size=3), Layout(name="mid", size=middle),
                      Layout(name="log", size=bottom), Layout(name="keys", size=3))
    stage = 0 if d.verb == "up" else 3
    status = {"plan": f"{t.spinner(now)} planning", "ready": "plan ready", "run": "",
              "done": "done", "failed": "failed"}[d.phase]
    right = f"{t.clock(now - d.t0)}  {status}".strip()
    if frozen:
        right, stage = f"built in {t.clock(d.run.elapsed)}", 1
    root["head"].update(t.header(STAGES, stage, right, width))
    left_w = int(width * 0.46)
    root["mid"].split_row(Layout(building_panel(d, left_w, middle, now), name="build", ratio=46),
                          Layout(cloud_panel(d, width - left_w, now), name="cloud", ratio=54))
    root["log"].update(log_panel(d, bottom - 2))
    if frozen:
        pairs = [("c", "back to the cluster")]
    elif d.phase == "ready":
        pairs = [("enter", "apply: creates real, billed resources"), ("q", "quit")]
    elif d.phase == "run":
        pairs = [("ctrl-c", "cancel the update")]
    elif d.phase == "plan":
        pairs = [("q", "quit")]
    else:
        pairs = [("enter", "continue"), ("q", "quit")]
    root["keys"].update(t.keys(pairs))
    return root


# ---------------------------------------------------------------------------- the live run


def platform_hosts() -> dict[str, dict[str, str]]:
    import yaml

    from chaos.cloud import REPO
    cfg = yaml.safe_load((REPO / "platform.yaml").read_text())
    return {h: dict(nets) for h, nets in cfg["network"]["hosts"].items()}


class Screen:
    def __init__(self, cfg: CloudConfig, verb: str, *, auto: bool, confirm: bool = True) -> None:
        self.cfg, self.verb, self.auto, self.confirm = cfg, verb, auto, confirm
        self.d = CloudDash(verb=verb, project="", region=cfg.region, run=Run(verb),
                           hosts=platform_hosts())
        self.stop = threading.Event()
        self.advance = threading.Event()
        self.result: dict[str, Any] = {}
        self.error: str = ""

    def probe_loop(self, probe: Probe) -> None:
        while not self.stop.is_set():
            self.d.snap = probe.snapshot()
            self.d.project = self.d.snap.project or self.d.project
            self.stop.wait(2.0)

    def work(self) -> None:
        d = self.d
        try:
            probe = Probe(self.cfg)
            threading.Thread(target=self.probe_loop, args=(probe,), daemon=True).start()
            if self.verb == "up":
                d.note("dim", "checking the external network for free floating IPs…")
                ok, why = probe.can_allocate_floating_ip()
                self.cfg.floating_ips = d.floating_ips = ok
                if ok:
                    d.note("ok", f"floating IPs: available on {why}")
                else:
                    short = "none free on the external network" if "Unable to find any IP" in why else why
                    d.note("warn", f"floating IPs: {short}; building without the 2 public addresses")
            pulumi = Pulumi(self.cfg)
            if self.verb == "up":
                self.plan(pulumi)
            else:
                held = pulumi.deployed()
                for r in held:
                    d.plan[r.parent] = d.plan.get(r.parent, 0) + 1
                d.planned = len(held)
                d.note("info", f"{len(held)} resources to delete, in the reverse of the order they were "
                               "created")
            d.phase = "ready"
            if self.confirm and not self.auto:
                self.advance.wait()
            else:
                time.sleep(3 if self.auto else 0)
            d.phase = "run"
            d.run = Run(self.verb)
            if self.verb == "up":
                d.run.resources.update(self.planned_resources)
                d.run.order.extend(self.planned_resources)
            pulumi.start(self.verb, d.run)
            code = pulumi.wait(d.run)
            time.sleep(1.0)
            self.d.snap = probe.snapshot()
            if code == 0:
                d.phase = "done"
                what = ("built", "created") if self.verb == "up" else ("destroyed", "deleted")
                d.note("ok", f"{what[0]} in {t.clock(d.run.elapsed)}: "
                             f"{d.run.counts['done']} resources {what[1]}")
            else:
                d.phase = "failed"
                d.note("bad", f"pulumi {self.verb} exited {code}")
            self.result = {"code": code, "elapsed": d.run.elapsed, "resources": len(d.run.resources),
                           "floating_ips": d.floating_ips}
        except Exception as e:  # noqa: BLE001 - shown on screen, then the caller decides
            d.phase = "failed"
            self.error = f"{type(e).__name__}: {e or traceback.format_exc().splitlines()[-3].strip()}"
            d.note("bad", self.error)
        finally:
            d.t_done = t.now()

    def plan(self, pulumi: Pulumi) -> None:
        """A preview first, so the bars know how big the job is before it starts."""
        d = self.d
        run = Run("preview")
        pulumi.start("preview", run)
        pulumi.wait(run)
        self.planned_resources = {u: r for u, r in run.resources.items()}
        for r in self.planned_resources.values():
            r.status, r.started, r.ended = "pending", 0.0, 0.0
        for r in self.planned_resources.values():
            d.plan[r.parent] = d.plan.get(r.parent, 0) + 1
        d.planned = len(self.planned_resources)
        d.note("info", f"plan: {d.planned} resources to create, nothing to change or delete")

    planned_resources: dict[str, Res] = {}


def run(cfg: CloudConfig, verb: str, *, auto: bool = False, confirm: bool = True) -> dict[str, Any]:
    """Show the screen until the step is finished and acknowledged; returns what happened."""
    console = Console()
    screen = Screen(cfg, verb, auto=auto, confirm=confirm)
    threading.Thread(target=screen.work, daemon=True).start()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        with Live(render(screen.d, console.size), console=console, screen=True, auto_refresh=False) as live:
            while True:
                if select.select([fd], [], [], 0.08)[0]:
                    ch = os.read(fd, 32).decode(errors="ignore")
                    if ch[:1] in ("q", "Q"):
                        screen.d.quit = True
                    elif ch[:1] in ("\r", "\n", " "):
                        screen.advance.set()
                        if screen.d.phase in ("done", "failed"):
                            break
                if screen.d.quit:
                    break
                if auto and screen.d.phase in ("done", "failed") and t.now() - screen.d.t_done > 6:
                    break
                live.update(render(screen.d, console.size), refresh=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        screen.stop.set()
    snap = screen.d.snap
    return {**screen.result, "quit": screen.d.quit, "error": screen.error,
            "snapshot": snap, "project": screen.d.project, "region": cfg.region, "dash": screen.d}


# ---------------------------------------------------------------------------- hand-over to the next scene


def dump(result: dict[str, Any], d: CloudDash | None = None) -> dict[str, Any]:
    """What the failover scene needs to show the cloud it runs 'inside': the final snapshot, the
    resources with their durations, and the plan. Plain JSON, so it crosses the process boundary."""
    snap: Snapshot | None = result.get("snapshot")
    out: dict[str, Any] = {"project": result.get("project", ""), "region": result.get("region", ""),
                           "elapsed": result.get("elapsed", 0.0),
                           "floating_ips": result.get("floating_ips", True),
                           "servers": [], "counts": {}, "quota": {}, "networks": [], "resources": [],
                           "plan": d.plan if d else {}, "planned": d.planned if d else 0,
                           "notes": [list(n) for n in d.notes] if d else [],
                           "policy": [d.run.policy_checks, d.run.policy_resources] if d else [0, 0]}
    if snap:
        out.update(servers=[{"name": s.name, "status": s.status, "flavor": s.flavor, "addresses": s.addresses,
                             "floating": s.floating} for s in snap.servers],
                   counts=snap.counts, quota={k: list(v) for k, v in snap.quota.items()},
                   networks=sorted(snap.networks))
    if d:
        base = d.run.started
        out["resources"] = [{"type": r.type, "name": r.name, "parent": r.parent, "status": r.status,
                             "started": r.started - base, "ended": r.ended - base}
                            for r in d.run.resources.values()]
    return out


def load(data: dict[str, Any]) -> CloudDash:
    d = CloudDash(verb="up", phase="done", project=data.get("project", ""), region=data.get("region", ""),
                  run=Run("up"), plan=data.get("plan", {}), planned=data.get("planned", 0),
                  floating_ips=data.get("floating_ips", True), hosts=platform_hosts())
    d.run.started, d.run.ended = 1.0, 1.0 + data.get("elapsed", 0.0)
    for i, r in enumerate(data.get("resources", [])):
        d.run.resources[str(i)] = Res(str(i), r["type"], r["name"], r["parent"], "create", r["status"],
                                      1.0 + r["started"], 1.0 + r["ended"])
    d.snap = Snapshot(0.0, d.project, d.region,
                      [Server("", s["name"], s["status"], s["flavor"], s["addresses"],
                              s.get("floating", "")) for s in data.get("servers", [])],
                      data.get("counts", {}),
                      quota={k: (v[0], v[1]) for k, v in data.get("quota", {}).items()},
                      networks=set(data.get("networks", [])))
    d.t0 = 0.0
    d.notes = [(level, text) for level, text in data.get("notes", [])]
    d.run.policy_checks, d.run.policy_resources = data.get("policy", [0, 0])
    return d

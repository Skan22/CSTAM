"""The interactive terminal dashboard behind `python -m chaos.demo`.

`render(dash, size)` is a pure function of a `Dash` (what the screen shows), so it is tested and
screenshotted without a lab. `Controller` keeps a `Dash` current from the live stack (polling the
gateways and the control plane, and streaming requests through the VIP) and runs the actions the
keys trigger; `run()` is the keyboard and refresh loop.
"""

from __future__ import annotations

import http.client
import os
import re
import select
import sys
import termios
import threading
import time
import tty
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from chaos.world import World

GW_COLOURS = {"gw-a": "cyan", "gw-b": "magenta"}
STATE_COLOURS = {"MASTER": "green", "BACKUP": "yellow", "FAULT": "red", "DOWN": "red",
                 "UNKNOWN": "grey50"}
KEYS = [("r", "register"), ("d", "delete"), ("n", "unknown host"), ("f", "fail over"),
        ("k", "kill primary VM"), ("x", "split brain"), ("h", "heal"), ("l", "rate"), ("q", "quit")]
RATES = (4, 15, 40)
SLUG = re.compile(r"[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?")

# Fixed rows in the layout; the event log gets what is left.
HEADER, GATEWAYS, TRAFFIC, FOOTER = 3, 11, 9, 3


@dataclass
class GwView:
    state: str = "UNKNOWN"
    vip: bool = False
    version: int | None = None
    faulted: bool = False
    up: bool = True
    traefik_pid: int | None = None
    cp_state: str = "UNKNOWN"  # what the control plane last heard from it
    priority: int = 0
    via: str = ""  # the address sandboxes see requests from


@dataclass
class TeamView:
    slug: str
    host: str
    ip: str
    state: str


@dataclass
class Sample:
    at: float  # monotonic, on completion
    host: str
    code: str  # an HTTP status, or ERR
    via: str  # gateway name, or ""
    ms: float


@dataclass
class Dash:
    domain: str = "example.test"
    vip: str = "0.0.0.0"
    gateways: dict[str, GwView] = field(default_factory=dict)
    teams: list[TeamView] = field(default_factory=list)
    samples: deque[Sample] = field(default_factory=lambda: deque(maxlen=2000))
    events: deque[tuple[float, str, str]] = field(default_factory=lambda: deque(maxlen=300))
    split_brain: bool = False
    caption: str = ""
    rate: int = RATES[0]
    prompt: tuple[str, str] | None = None  # (what, typed so far)
    busy: str = ""
    quit: bool = False
    t0: float = field(default_factory=time.monotonic)

    def log(self, text: str, level: str = "info") -> None:
        self.events.append((time.monotonic() - self.t0, level, text))


# ---------------------------------------------------------------------------- rendering


def badge(state: str) -> Text:
    return Text(f" {state} ", style=f"bold black on {STATE_COLOURS.get(state, 'white')}")


def gateway_panel(name: str, g: GwView, vip: str) -> Panel:
    state = "DOWN" if not g.up else g.state
    colour = STATE_COLOURS.get(state, "white")
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="dim")
    body.add_column()
    body.add_row("vrrp", badge(state))
    body.add_row("vip", Text(f"◉ holds {vip}", style="bold green") if g.vip
                 else Text("○ -", style="dim"))
    body.add_row("config", Text(f"v{g.version}" if g.version is not None else "?",
                                style="bold"))
    body.add_row("agent", Text("● up", style="green") if g.up and not g.faulted
                 else Text("✖ faulted" if g.faulted else "✖ down", style="red"))
    body.add_row("traefik", Text(f"pid {g.traefik_pid}" if g.traefik_pid else "-"))
    body.add_row("cp sees", Text(g.cp_state, style=STATE_COLOURS.get(g.cp_state, "white")))
    return Panel(body, title=f"[bold {GW_COLOURS.get(name, 'white')}]{name}[/]",
                 subtitle=f"priority {g.priority}", border_style=colour, box=box.HEAVY if g.vip
                 else box.ROUNDED)


def cluster_panel(d: Dash) -> Panel:
    masters = [n for n, g in d.gateways.items() if g.up and g.state == "MASTER"]
    body = Table.grid(padding=(0, 1))
    body.add_column(justify="right", style="dim")
    body.add_column()
    body.add_row("vip", Text(d.vip, style="bold"))
    body.add_row("masters", Text(", ".join(masters) or "none",
                                 style="green" if len(masters) == 1 else "bold red"))
    if d.split_brain or len(masters) > 1:
        body.add_row("", Text(" SPLIT BRAIN ", style="bold white on red blink"))
    else:
        body.add_row("", Text("healthy" if masters else "NO MASTER",
                              style="green" if masters else "bold red"))
    body.add_row("teams", Text(str(len(d.teams))))
    border = "red" if d.split_brain or len(masters) != 1 else "green"
    return Panel(body, title="[bold]cluster[/]", border_style=border)


def window(d: Dash, seconds: float, now: float) -> list[Sample]:
    return [s for s in d.samples if now - s.at <= seconds]


def traffic_panel(d: Dash, width: int, now: float) -> Panel:
    inner = max(10, width - 4)
    recent = list(d.samples)[-2 * inner:]
    strip = Text()
    for i, s in enumerate(recent):
        if i and i % inner == 0:
            strip.append("\n")
        if s.code == "ERR":
            strip.append("✖", style="bold red")
        elif s.code != "200":
            strip.append("▮", style="yellow")
        else:
            strip.append("▮", style=GW_COLOURS.get(s.via, "white"))
    win = window(d, 5, now)
    ok = [s for s in win if s.code == "200"]
    errs = [s for s in win if s.code == "ERR"]
    other = [s for s in win if s.code not in ("200", "ERR")]
    lat = sorted(s.ms for s in ok)
    p50 = lat[len(lat) // 2] if lat else 0.0
    p99 = lat[min(len(lat) - 1, int(len(lat) * 0.99))] if lat else 0.0
    shares = Text()
    for name in d.gateways:
        n = sum(1 for s in ok if s.via == name)
        pct = 100 * n / len(ok) if ok else 0
        shares.append(f" {name} ", style=f"bold {GW_COLOURS.get(name, 'white')}")
        shares.append("█" * round(pct / 5) + "░" * (20 - round(pct / 5)),
                      style=GW_COLOURS.get(name, "white"))
        shares.append(f" {pct:3.0f}%  ")
    done = [s for s in d.samples if now - s.at <= 60 and s.code == "200"]
    gap = max((b.at - a.at for a, b in zip(done, done[1:], strict=False)), default=0.0)
    stats = Text()
    stats.append(f" {len(win) / 5:4.1f} req/s ", style="bold")
    stats.append(f"  errors {len(errs)}", style="bold red" if errs else "green")
    stats.append(f"  404s {len(other)}", style="yellow" if other else "dim")
    stats.append(f"  p50 {p50:.0f}ms  p99 {p99:.0f}ms")
    stats.append(f"  longest silence (60s) {gap:.2f}s",
                 style="bold red" if gap > 0.8 else "dim")
    return Panel(Group(strip, Text(""), shares, stats), title="[bold]live requests through the VIP[/]",
                 subtitle=f"[dim]{d.rate} req/s · each ▮ is one request, coloured by the gateway "
                          "that served it · ▮ 404 · ✖ failed[/]", border_style="blue")


def routes_panel(d: Dash, now: float) -> Panel:
    t = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False)
    t.add_column("subdomain", overflow="ellipsis", no_wrap=True, ratio=1)
    t.add_column("sandbox", no_wrap=True, min_width=10)
    t.add_column("state", no_wrap=True, min_width=8)
    t.add_column("5s", justify="right", no_wrap=True, min_width=8)
    win = window(d, 5, now)
    for team in d.teams:
        mine = [s for s in win if s.host == team.host]
        oks = sum(1 for s in mine if s.code == "200")
        colour = {"active": "green", "pending": "yellow", "draining": "yellow"}.get(team.state, "red")
        t.add_row(team.host.removesuffix("." + d.domain) + f"[dim].{d.domain}[/]", team.ip,
                  f"[{colour}]{team.state}[/]", f"{oks}/{len(mine)} ok" if mine else "-")
    if not d.teams:
        t.add_row("[dim]no teams yet - press r[/]", "", "", "")
    return Panel(t, title="[bold]routes[/]", border_style="cyan")


def events_panel(d: Dash, lines: int) -> Panel:
    out = Text()
    styles = {"info": "white", "ok": "green", "warn": "yellow", "bad": "bold red", "act": "bold cyan"}
    shown = list(d.events)[-lines:]
    for i, (t, level, text) in enumerate(shown):
        out.append(f"{t:7.1f}s ", style="dim")
        out.append(text, style=styles.get(level, "white"))
        if i < len(shown) - 1:
            out.append("\n")
    return Panel(out, title="[bold]events[/]", border_style="grey50")


def footer(d: Dash) -> Panel:
    if d.prompt:
        what, typed = d.prompt
        body: Text = Text.assemble((f" {what} › ", "bold cyan"), (typed, "bold"), ("▌", "blink"),
                                   ("   enter to confirm · esc to cancel", "dim"))
    else:
        body = Text()
        for key, label in KEYS:
            body.append(f" {key} ", style="bold black on white")
            body.append(f" {label}  ", style="dim")
        if d.busy:
            body.append(f"  ⏳ {d.busy}", style="bold yellow")
    return Panel(body, border_style="grey37", box=box.SQUARE)


def render(d: Dash, size: tuple[int, int], now: float | None = None) -> Layout:
    width, height = size
    now = time.monotonic() if now is None else now
    root = Layout()
    middle = max(5, height - HEADER - GATEWAYS - TRAFFIC - FOOTER)
    root.split_column(
        Layout(name="header", size=HEADER), Layout(name="gateways", size=GATEWAYS),
        Layout(name="traffic", size=TRAFFIC), Layout(name="bottom", size=middle),
        Layout(name="footer", size=FOOTER))
    head = Text.assemble(("  IPO ", "bold white on blue"), ("  Resilient IP Optimizer  ", "bold"),
                         (f"  real Traefik · keepalived · ipo-agent · *.{d.domain}", "dim"))
    root["header"].update(Panel(Align.left(head), border_style="blue", box=box.HEAVY))
    gws = list(d.gateways.items())
    root["gateways"].split_row(*[Layout(gateway_panel(n, g, d.vip), name=n) for n, g in gws],
                               Layout(cluster_panel(d), name="cluster"))
    root["traffic"].update(traffic_panel(d, width, now))
    root["bottom"].split_row(Layout(routes_panel(d, now), name="routes", ratio=5),
                             Layout(events_panel(d, middle - 2), name="events", ratio=4))
    if d.caption:
        root["header"].update(Panel(Text(d.caption, style="bold yellow"), title="[bold]narration[/]",
                                    border_style="yellow", box=box.HEAVY))
    root["footer"].update(footer(d))
    return root


# ---------------------------------------------------------------------------- the live stack


class Controller:
    """Keeps `dash` current and performs the actions behind the keys."""

    def __init__(self, w: World, dash: Dash) -> None:
        from lab.stack import EDGE_CLIENT

        self.w, self.d = w, dash
        self.edge_client = EDGE_CLIENT
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=48, thread_name_prefix="req")
        self.actions = ThreadPoolExecutor(max_workers=4, thread_name_prefix="act")
        self.via = {g.addr["sandbox"]: name for name, g in w.gateways.items()}
        for name, g in w.gateways.items():
            dash.gateways[name] = GwView(
                priority=w.plat["vrrp"]["priority_a" if name == "gw-a" else "priority_b"],
                via=g.addr["sandbox"])
        dash.domain, dash.vip = w.plat["domain"], w.vip
        self._last: dict[str, Any] = {}

    def start(self) -> None:
        for fn in (self.poll_loop, self.stream_loop):
            threading.Thread(target=fn, daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.actions.shutdown(wait=False, cancel_futures=True)

    # -- observation
    def poll_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.poll()
            except Exception as e:  # noqa: BLE001 - a half-dead lab must not stop the display
                self.d.log(f"poll: {type(e).__name__}: {e}", "warn")
            self.stop.wait(0.4)

    def poll(self) -> None:
        d, w = self.d, self.w
        cp: dict[str, str] = {}
        code, body = w.cp.api("GET", "/v1/gateways", timeout=2)
        if code == 200:
            cp = {g["gateway"]: g["vrrp_state"] for g in body["gateways"]}
            d.split_brain = bool(body["split_brain"])
        for name, g in w.gateways.items():
            view = d.gateways[name]
            view.up = not g.off and all(
                (p := g.procs.get(n)) is not None and p.poll() is None
                for n in ("agent", "traefik", "keepalived"))
            view.state = g.vrrp_state() if not g.off else "DOWN"
            view.vip = g.has_vip()
            view.cp_state = cp.get(name, "UNKNOWN")
            tp = g.procs.get("traefik")
            view.traefik_pid = tp.pid if tp and tp.poll() is None else None
            try:
                st = g.status()
                view.version, view.faulted = st.get("live_version"), bool(st.get("faulted"))
            except Exception:  # noqa: BLE001 - the agent is gone with the machine
                view.faulted = False
            self.note_changes(name, view)
        code, teams = w.cp.api("GET", "/v1/teams", timeout=2)
        if code == 200:
            d.teams = [TeamView(t["slug"], t["subdomain"], t.get("ip") or "-", t["state"])
                       for t in teams if t["state"] != "deleted"]

    def note_changes(self, name: str, v: GwView) -> None:
        last = self._last.setdefault(name, {})
        shown = "DOWN" if not v.up and v.state == "DOWN" else v.state
        for key, now, text in (("state", shown, f"{name}: {last.get('state')} → {shown}"),
                               ("vip", v.vip, f"{name} {'took' if v.vip else 'released'} the VIP"),
                               ("version", v.version, f"{name} now runs config v{v.version}")):
            if key in last and last[key] != now and now is not None:
                level = "ok" if now in ("MASTER", True) else "warn" if now in (
                    "FAULT", "DOWN", False) else "info"
                self.d.log(text, level)
            last[key] = now

    # -- traffic
    def stream_loop(self) -> None:
        nxt = time.monotonic()
        n = 0
        while not self.stop.is_set():
            hosts = [t.host for t in self.d.teams if t.state == "active"]
            if hosts:
                n += 1
                try:
                    self.pool.submit(self.one_request, hosts[n % len(hosts)])
                except RuntimeError:
                    return
            nxt += 1.0 / self.d.rate
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()  # fell behind: do not burst to catch up

    def request(self, host: str, timeout: float = 1.0) -> Sample:
        t = time.monotonic()
        try:
            c = http.client.HTTPConnection(self.w.vip, 80, timeout=timeout,
                                           source_address=(self.edge_client, 0))
            c.request("GET", "/", headers={"Host": host, "Connection": "close"})
            r = c.getresponse()
            body = r.read().decode(errors="replace")
            c.close()
            m = re.search(r"via=(\S+)", body)
            return Sample(time.monotonic(), host, str(r.status),
                          self.via.get(m.group(1), m.group(1)) if m else "", (time.monotonic() - t) * 1000)
        except (OSError, http.client.HTTPException):
            return Sample(time.monotonic(), host, "ERR", "", (time.monotonic() - t) * 1000)

    def one_request(self, host: str) -> None:
        self.d.samples.append(self.request(host))

    # -- actions
    def run(self, label: str, fn: Callable[[], None]) -> None:
        def go() -> None:
            self.d.busy = label
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - shown, not fatal
                self.d.log(f"{label} failed: {type(e).__name__}: {e}", "bad")
            finally:
                self.d.busy = ""
        self.actions.submit(go)

    def register(self, slug: str) -> None:
        from lab.stack import wait_for

        host = f"{slug}.{self.d.domain}"
        t0 = time.monotonic()
        self.d.log(f"POST /v1/teams {{slug: {slug}}}", "act")
        team = self.w.cp.register(slug)
        self.d.log(f"accepted: sandbox {team['ip']} ({team['path']} path)", "info")
        self.w.cp.wait_active(slug)
        wait_for(lambda: self.request(host, 1.0).code == "200", f"{host} to answer", 40, 0.1)
        self.d.log(f"{host} answers 200, {time.monotonic() - t0:.1f}s after the request, no restart",
                   "ok")

    def delete(self, slug: str) -> None:
        from lab.stack import wait_for

        team = next((t for t in self.d.teams if t.slug == slug), None)
        if team is None:
            self.d.log(f"no team called {slug}", "warn")
            return
        _, teams = self.w.cp.api("GET", "/v1/teams")
        tid = next(t["id"] for t in teams if t["slug"] == slug)
        self.d.log(f"DELETE /v1/teams/{tid[:8]}… ({slug})", "act")
        self.w.cp.api("DELETE", f"/v1/teams/{tid}")
        wait_for(lambda: self.request(team.host).code == "404", "the route to go", 60, 0.2)
        self.d.log(f"{team.host} now answers 404: route removed", "ok")

    def unknown(self) -> None:
        host = f"nobody-{int(time.time()) % 1000}.{self.d.domain}"
        s = self.request(host)
        self.d.log(f"GET http://{host}/ → {s.code} (no such team)", "warn" if s.code == "404" else "bad")

    def failover(self) -> None:
        master = next((n for n, g in self.d.gateways.items() if g.state == "MASTER" and g.up), None)
        self.d.log(f"POST /v1/gateways/failover (primary is {master})", "act")
        code, body = self.w.cp.api("POST", "/v1/gateways/failover", {})
        self.d.log(f"→ {code} {body}", "info" if code == 202 else "bad")

    def kill_primary(self) -> None:
        master = next((g for g in self.w.gateways.values() if g.vrrp_state() == "MASTER"), None)
        if master is None:
            self.d.log("no MASTER to kill", "warn")
            return
        self.d.log(f"power off {master.name}: every process dies, the NICs go dark", "act")
        master.power_off()

    def partition(self) -> None:
        self.d.log("dropping VRRP between the gateways (they stay up, but cannot hear each other)", "act")
        self.w.partition()

    def heal(self) -> None:
        self.d.log("healing: undo the partition, restart whatever died", "act")
        self.w.heal()
        self.d.log("cluster healthy: one MASTER, one BACKUP", "ok")

    def cycle_rate(self) -> None:
        self.d.rate = RATES[(RATES.index(self.d.rate) + 1) % len(RATES)]
        self.d.log(f"request rate {self.d.rate}/s", "info")

    def key(self, ch: str) -> None:
        d = self.d
        if d.prompt:
            what, typed = d.prompt
            if ch in ("\r", "\n"):
                d.prompt = None
                if SLUG.fullmatch(typed):
                    fn = self.register if what == "register team" else self.delete
                    self.run(f"{what} {typed}", lambda: fn(typed))
                elif typed:
                    d.log(f"'{typed}' is not a valid subdomain label", "warn")
            elif ch == "\x1b":
                d.prompt = None
            elif ch in ("\x7f", "\b"):
                d.prompt = (what, typed[:-1])
            elif ch.isprintable() and len(typed) < 30:
                d.prompt = (what, typed + ch.lower())
            return
        table: dict[str, Callable[[], None]] = {
            "r": lambda: setattr(d, "prompt", ("register team", "")),
            "d": lambda: setattr(d, "prompt", ("delete team", "")),
            "n": lambda: self.run("probe", self.unknown),
            "f": lambda: self.run("failover", self.failover),
            "k": lambda: self.run("kill primary", self.kill_primary),
            "x": lambda: self.run("partition", self.partition),
            "h": lambda: self.run("healing", self.heal),
            "l": self.cycle_rate,
            "q": lambda: setattr(d, "quit", True),
        }
        if (action := table.get(ch.lower())) is not None:
            action()


# ---------------------------------------------------------------------------- scripted tour


def autopilot(c: Controller) -> None:
    """The same actions the keys trigger, narrated, for a hands-free recording."""
    d = c.d

    def say(text: str, hold: float = 4.0) -> None:
        d.caption = text
        c.stop.wait(hold)

    def do(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            d.log(f"{type(e).__name__}: {e}", "bad")

    say("Two gateways share one virtual IP. keepalived decides who holds it; Traefik routes by Host.")
    say("Scene 1 - subdomain routing: a registered team answers, an unknown name gets 404", 2)
    do(c.unknown)
    c.stop.wait(2)
    say("Scene 2 - hot reload: register two teams while requests flow; no process restarts", 2)
    do(lambda: c.register("alpha"))
    do(lambda: c.register("bravo"))
    say("Both are live. The signed config reached both gateways, verified and probed, before the swap.", 5)
    do(lambda: c.delete("bravo"))
    say("bravo's route is gone the same way, and alpha never noticed.", 4)
    say("Scene 3 - failover: the control plane faults the primary; watch the stream", 3)
    do(c.failover)
    say("The VIP moved in a fraction of a second. Every request kept being answered.", 6)
    say("Clear the fault: the old primary rejoins as BACKUP and does not take the VIP back", 2)
    do(c.heal)
    say("Harder: power off the primary's VM (processes die, NICs go dark)", 3)
    do(c.kill_primary)
    say("The other gateway took over. Only the requests in flight at that instant could notice.", 6)
    say("Bring the dead VM back: it boots, runs keepalived, and rejoins as BACKUP", 2)
    do(c.heal)
    say("Scene 4 - split brain: cut the heartbeat between healthy gateways", 3)
    do(c.partition)
    say("Both claim the VIP. The control plane hears from both and raises the alarm.", 7)
    do(c.heal)
    say("Healed: one MASTER, one BACKUP. That is the resilience story. Press q to leave.", 6)
    d.quit = True


# ---------------------------------------------------------------------------- the loop


def run(w: World, *, auto: bool = False) -> None:
    console = Console()
    dash = Dash()
    ctl = Controller(w, dash)
    ctl.start()
    dash.log("booted: two gateways, a sandbox backend, the control plane", "ok")
    if auto:
        threading.Thread(target=autopilot, args=(ctl,), daemon=True).start()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        from rich.live import Live

        with Live(render(dash, console.size), console=console, screen=True,
                  auto_refresh=False) as live:
            while not dash.quit:
                if select.select([fd], [], [], 0.1)[0]:
                    data = os.read(fd, 32).decode(errors="ignore")
                    for ch in ([data] if data == "\x1b" else [] if data.startswith("\x1b")
                               else list(data)):
                        ctl.key(ch)
                live.update(render(dash, console.size), refresh=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        ctl.close()

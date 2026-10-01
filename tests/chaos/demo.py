"""A recordable demo on the real stack.

    uv run python -m chaos.demo          interactive dashboard: you press the keys (chaos/tui.py)
    uv run python -m chaos.demo --auto   the same dashboard, driven by a narrated script
    uv run python -m chaos.demo --tour   plain scrolling text, one scene per Enter (DEMO_PACE=s
                                         advances by itself); what runs when stdout is not a terminal

Boots the same lab as the chaos suite (real keepalived, Traefik and ipo-agent on two gateways, a
sandbox backend, the real control plane). The scenes:

  1. subdomain routing: each team's <slug>.<domain> reaches its sandbox, unknown names get 404;
  2. hot reload: a team is registered and removed while traffic flows, and its subdomain starts
     and stops answering with no restart (the Traefik process id is shown not to change);
  3. failover: a live request stream through the VIP while the control plane faults the primary,
     so the stream moves to the other gateway (`via=` is the gateway that forwarded it).
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from chaos.world import World, boot

if TYPE_CHECKING:
    from chaos.cloud import CloudConfig
from lab import netns, tools
from lab.stack import Postgres, wait_for

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def say(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}", flush=True)


def note(text: str) -> None:
    print(f"{DIM}{text}{RESET}", flush=True)


def pause(prompt: str = "press Enter for the next scene") -> None:
    pace = os.environ.get("DEMO_PACE")
    if pace:
        time.sleep(float(pace))
    elif sys.stdin.isatty():
        input(f"{DIM}-- {prompt} --{RESET}")


def request(w: World, host: str) -> tuple[str, str]:
    """(status, body) of one request for `host` through the VIP."""
    p = netns.run(["curl", "-s", "-m", "2", "-w", "\n%{http_code}", "-H", f"Host: {host}",
                   f"http://{w.vip}/"], check=False)
    body, _, code = p.stdout.rpartition("\n")
    return code or "---", body.strip()


def show(w: World, host: str) -> bool:
    code, body = request(w, host)
    ok = code == "200"
    colour = GREEN if ok else RED
    print(f"  GET http://{host}/  ->  {colour}{code}{RESET}  {body}", flush=True)
    return ok


def gateways(w: World) -> None:
    for name, g in w.gateways.items():
        state = g.vrrp_state()
        colour = GREEN if state == "MASTER" else YELLOW if state == "BACKUP" else RED
        vip = "  holds the VIP" if g.has_vip() else ""
        live = g.status().get("live_version", "?")
        print(f"  {name}: {colour}{state:<6}{RESET} config v{live}{vip}", flush=True)


def traefik_pids(w: World) -> dict[str, int]:
    return {n: g.procs["traefik"].pid for n, g in w.gateways.items()}


def scene_routing(w: World) -> None:
    say("Scene 1 - active subdomain routing")
    note("Both gateways run Traefik; the control plane compiled one route per team.")
    gateways(w)
    note("\nA registered team's subdomain reaches its sandbox VM; anything else is a 404:")
    show(w, w.host)
    show(w, f"nobody.{w.plat['domain']}")


def scene_hot_reload(w: World) -> None:
    say("Scene 2 - dynamic hot reload")
    host = f"demo.{w.plat['domain']}"
    before = traefik_pids(w)
    note(f"Traefik process ids (must not change): {before}")
    note(f"\nbefore registering: {host}")
    show(w, host)
    t0 = time.monotonic()
    team = w.cp.register("demo")
    note(f"\nPOST /v1/teams  ->  team {team['team_id']} at {team['ip']} (job {team['job_id']})")
    w.cp.wait_active("demo")
    wait_for(lambda: request(w, host)[0] == "200", "the new subdomain to answer", 30, 0.1)
    print(f"  {GREEN}live {time.monotonic() - t0:.1f}s after the request{RESET}", flush=True)
    show(w, host)
    gateways(w)
    note("\nThe agent verified the signed config, swapped it in and probed the route; Traefik")
    note("reloaded its file provider without restarting:")
    assert traefik_pids(w) == before
    print(f"  Traefik process ids unchanged: {traefik_pids(w)}", flush=True)
    note("\nNow remove the team; its route disappears the same way:")
    code, _ = w.cp.api("DELETE", f"/v1/teams/{team['team_id']}")
    wait_for(lambda: request(w, host)[0] == "404", "the route to be removed", 60, 0.2)
    show(w, host)
    show(w, w.host)


def scene_failover(w: World) -> None:
    say("Scene 3 - simulated primary gateway failover")
    old = w.master()
    new = w.other(old)
    gateways(w)
    note(f"\nA request every 0.2s through the VIP; `via=` is the address the sandbox saw the request "
         f"come from ({old.name} is {old.addr['sandbox']}, {new.name} is {new.addr['sandbox']}).")
    stop = threading.Event()
    log: list[tuple[float, str, str]] = []

    def stream() -> None:
        while not stop.is_set():
            code, body = request(w, w.host)
            log.append((time.monotonic(), code, body))
            via = body.split("via=")[-1].split()[0] if "via=" in body else "-"
            colour = GREEN if code == "200" else RED
            print(f"    {colour}{code}{RESET} via={via}", flush=True)
            time.sleep(0.2)

    th = threading.Thread(target=stream)
    th.start()
    time.sleep(2)
    say(f"POST /v1/gateways/failover   (the control plane faults {old.name}, the current MASTER)")
    t0 = time.monotonic()
    code, body = w.cp.api("POST", "/v1/gateways/failover", {})
    print(f"  -> {code} {body}", flush=True)
    wait_for(lambda: new.vrrp_state() == "MASTER", f"{new.name} to take over", 10, 0.05)
    note(f"\n{new.name} is MASTER {time.monotonic() - t0:.2f}s after the request")
    time.sleep(3)
    stop.set()
    th.join()
    failed = [c for _, c, _ in log if c != "200"]
    longest = max((b - a for (a, _, _), (b, _, _) in zip(log, log[1:], strict=False)), default=0)
    print(f"\n  {len(log)} requests, {len(failed)} failed, longest silence {longest:.2f}s", flush=True)
    gateways(w)
    note(f"\nSplit brain reported by the control plane: {w.cp.split_brain()}")
    say(f"Recovering {old.name}")
    from lab.stack import http_json
    http_json("DELETE", f"{old.agent_url}/fault")
    wait_for(lambda: old.vrrp_state() == "BACKUP", f"{old.name} to rejoin as BACKUP", 15, 0.2)
    note("keepalived runs nopreempt, so the VIP stays on the new primary:")
    gateways(w)


def run(workdir: Path, dsn: str) -> None:
    say("Booting two gateways (keepalived + Traefik + ipo-agent), a sandbox backend and the control plane...")
    w = boot(workdir, dsn)
    try:
        scene_routing(w)
        pause()
        scene_hot_reload(w)
        pause()
        scene_failover(w)
        say("Done.")
    finally:
        w.shutdown()


def interactive(workdir: Path, dsn: str, *, auto: bool) -> None:
    from chaos import tui

    w = boot(workdir, dsn)
    try:
        tui.run(w, auto=auto)
    finally:
        w.shutdown()


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m chaos.demo", description=__doc__.split("\n\n")[0])
    ap.add_argument("--auto", action="store_true", help="drive the dashboards from a narrated script")
    ap.add_argument("--tour", action="store_true", help="plain scrolling text, one scene per Enter")
    ap.add_argument("--clouds", type=Path, default=os.environ.get("IPO_CLOUDS"),
                    help="a clouds.yaml: build the platform in that real cloud first (pulumi up) and "
                         "tear it down at the end; without it the demo runs on the local lab only")
    ap.add_argument("--cloud", default="", help="which cloud in the clouds.yaml (default: the first)")
    ap.add_argument("--keep", action="store_true", help="leave the cloud built when the demo ends")
    ap.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse(argv)
    if args.inner:
        workdir, dsn = Path(os.environ["IPO_LAB_WORKDIR"]), os.environ["IPO_LAB_DSN"]
        if args.tour or not sys.stdout.isatty():
            run(workdir, dsn)
        else:
            interactive(workdir, dsn, auto=args.auto)
        return 0
    if not tools.can_unshare():
        print("unprivileged user namespaces are not available here", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="ipo-demo-", dir=os.environ.get("IPO_LAB_TMP")))
    cfg = None
    built = False
    snapshot = work / "cloud.json"
    try:
        if args.clouds:
            from chaos import cloudtui
            from chaos.cloud import CloudConfig

            cfg = CloudConfig(args.clouds, args.cloud)
            result = cloudtui.run(cfg, "up", auto=args.auto)
            built = bool(result.get("resources"))
            if result.get("quit") or result.get("code") not in (0, None) or result.get("error"):
                why = result.get("error") or f"exit {result.get('code')}"
                print(f"the build did not finish ({why})", file=sys.stderr)
                return 1
            snapshot.write_text(json.dumps(cloudtui.dump(result, result.get("dash"))))
        return lab_scene(work, args, snapshot if args.clouds else None)
    finally:
        if cfg is not None and built:
            teardown(cfg, args)


def lab_scene(work: Path, args: argparse.Namespace, snapshot: Path | None) -> int:
    pg = Postgres(work / "pg")
    pg.start()
    try:
        env = {**os.environ, "IPO_LAB_DSN": pg.dsn, "IPO_LAB_WORKDIR": str(work / "lab")}
        if snapshot:
            env["IPO_CLOUD_SNAPSHOT"] = str(snapshot)
        flags = [f for f in ("--auto", "--tour") if getattr(args, f[2:])]
        cmd = netns.in_lab([sys.executable, "-m", "chaos.demo", "--inner", *flags])
        return subprocess.run(cmd, env=env, cwd=Path(__file__).parents[1]).returncode
    finally:
        pg.stop()


def teardown(cfg: "CloudConfig", args: argparse.Namespace) -> None:
    from chaos import cloudtui

    if args.keep:
        print("\nthe cloud is still built (--keep). When you are done: "
              f"python -m chaos.cloud destroy --clouds {cfg.clouds_file}", file=sys.stderr)
        return
    if not args.auto:
        answer = input("\nDestroy everything pulumi built in the cloud now? [Y/n] ").strip().lower()
        if answer in ("n", "no"):
            print(f"left built. Destroy later: python -m chaos.cloud destroy --clouds {cfg.clouds_file}",
                  file=sys.stderr)
            return
    cloudtui.run(cfg, "destroy", auto=args.auto, confirm=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

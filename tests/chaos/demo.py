"""A narrated, recordable demo on the real stack: `uv run python -m chaos.demo`.

Boots the same lab as the chaos suite (real keepalived, Traefik and ipo-agent on two gateways, a
sandbox backend, the real control plane) and walks through three scenes, pausing for Enter between
them when run in a terminal (DEMO_PACE=seconds auto-advances instead):

  1. subdomain routing: each team's <slug>.<domain> reaches its sandbox, unknown names get 404;
  2. hot reload: a team is registered and removed while traffic flows, and its subdomain starts
     and stops answering with no restart (the Traefik process id is shown not to change);
  3. failover: a live request stream through the VIP while the control plane faults the primary,
     so the stream moves to the other gateway (`via=` is the gateway that forwarded it).
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from chaos.world import World, boot
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


def main(argv: list[str]) -> int:
    if argv[:1] == ["--inner"]:
        run(Path(os.environ["IPO_LAB_WORKDIR"]), os.environ["IPO_LAB_DSN"])
        return 0
    if not tools.can_unshare():
        print("unprivileged user namespaces are not available here", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="ipo-demo-", dir=os.environ.get("IPO_LAB_TMP")))
    pg = Postgres(work / "pg")
    pg.start()
    try:
        env = {**os.environ, "IPO_LAB_DSN": pg.dsn, "IPO_LAB_WORKDIR": str(work / "lab")}
        cmd = netns.in_lab([sys.executable, "-m", "chaos.demo", "--inner"])
        return subprocess.run(cmd, env=env, cwd=Path(__file__).parents[1]).returncode
    finally:
        pg.stop()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

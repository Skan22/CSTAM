"""The demo's real cloud: runs `pulumi up` / `destroy` against FelCloud and reports what exists.

Nothing here touches the repository's stacks: the demo uses its own stack (`live`) with a local
file backend, a generated passphrase and a generated SSH key under `~/.local/share/ipo-demo`.
Credentials stay where the user keeps them (`--clouds`); only their path is passed on.

`python -m chaos.cloud up|destroy|status --clouds PATH` runs one step with plain text output.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
PULUMI_DIR = REPO / "infra" / "pulumi"
STACK = "live"
DEFAULT_STATE = Path(os.environ.get("IPO_DEMO_STATE", Path.home() / ".local/share/ipo-demo"))


@dataclass
class CloudConfig:
    clouds_file: Path
    cloud: str = ""
    state_dir: Path = DEFAULT_STATE
    floating_ips: bool = True

    def __post_init__(self) -> None:
        self.clouds_file = Path(self.clouds_file).expanduser().resolve()
        if not self.cloud:
            self.cloud = next(iter(yaml.safe_load(self.clouds_file.read_text())["clouds"]))

    @property
    def region(self) -> str:
        info = yaml.safe_load(self.clouds_file.read_text())["clouds"][self.cloud]
        return str(info.get("region_name", ""))

    def config_flags(self) -> list[str]:
        """Stack config passed on the command line, so no value lands in the repository."""
        pub = (self.state_dir / "ssh_ed25519.pub").read_text().strip()
        return ["--config", f"ipo:publicKey={pub}",
                "--config", f"ipo:floatingIps={'true' if self.floating_ips else 'false'}",
                "--config", "ipo:antiAffinity=soft-anti-affinity"]

    def prepare(self) -> dict[str, str]:
        """Create the state directory, passphrase and SSH key once; return Pulumi's environment."""
        for tool in ("pulumi", "ssh-keygen"):
            if not shutil.which(tool):
                raise RuntimeError(f"{tool} is not installed")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.state_dir / "pulumi").mkdir(exist_ok=True)
        passphrase = self.state_dir / "passphrase"
        if not passphrase.exists():
            passphrase.write_text(secrets.token_urlsafe(32))
            passphrase.chmod(0o600)
        key = self.state_dir / "ssh_ed25519"
        if not key.exists():
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ipo-demo", "-f", str(key)],
                           check=True)
        return {
            **os.environ,
            "PULUMI_BACKEND_URL": f"file://{self.state_dir / 'pulumi'}",
            "PULUMI_CONFIG_PASSPHRASE_FILE": str(passphrase),
            "PULUMI_SKIP_UPDATE_CHECK": "true",
            "PULUMI_DEBUG_COMMANDS": "true",  # unhides --event-log, the stream the screen follows
            "OS_CLIENT_CONFIG_FILE": str(self.clouds_file),
            "OS_CLOUD": self.cloud,
        }


# ------------------------------------------------------------------ pulumi events


@dataclass
class Res:
    urn: str
    type: str
    name: str
    parent: str  # the component it belongs to, e.g. GatewayPair; "" at the top
    op: str
    status: str = "pending"  # pending, working, done, failed
    started: float = 0.0
    ended: float = 0.0
    error: str = ""

    @property
    def kind(self) -> str:
        return self.type.rsplit(":", 1)[-1]


def split_urn(urn: str) -> tuple[str, str, str]:
    head, _, name = urn.rpartition("::")
    chain = head.rpartition("::")[2].split("$")
    typ = chain[-1]
    parent = next((t.rsplit(":", 1)[-1] for t in reversed(chain[:-1]) if t.startswith("ipo:")), "")
    return typ, name, parent


@dataclass
class Run:
    """One `pulumi up` or `destroy`: resources by urn, the log, and the outcome."""

    verb: str
    resources: dict[str, Res] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    policies: int = 0
    policy_checks: int = 0  # individual guardrail evaluations that passed
    policy_resources: int = 0  # resources they were evaluated on
    started: float = 0.0
    ended: float = 0.0
    exit: int | None = None
    summary: dict[str, int] = field(default_factory=dict)

    def handle(self, ev: dict[str, Any]) -> None:
        now = time.monotonic()
        if pre := ev.get("resourcePreEvent"):
            m = pre["metadata"]
            if m["type"].startswith(("pulumi:", "ipo:")) or m["op"] in ("same", "read"):
                return  # the stack, providers and our own grouping components are not cloud resources
            typ, name, parent = split_urn(m["urn"])
            r = self.resources.setdefault(m["urn"], Res(m["urn"], typ, name, parent, m["op"]))
            if r.status == "pending":
                self.order.append(m["urn"])
            r.op, r.status, r.started = m["op"], "working", now
        elif out := ev.get("resOutputsEvent"):
            done = self.resources.get(out["metadata"]["urn"])
            if done and done.status != "failed":
                done.status, done.ended = "done", now
        elif diag := ev.get("diagnosticEvent"):
            d = diag
            msg = str(d.get("message", "")).strip()
            if d.get("severity") == "error" and msg:
                self.errors.append(msg.splitlines()[0][:300])
                bad = self.resources.get(d.get("urn") or "")
                if bad:
                    bad.status, bad.ended, bad.error = "failed", now, msg.splitlines()[0][:200]
            elif msg and d.get("severity") in ("warning", "info#err"):
                self.log.append(msg.splitlines()[0][:200])
        elif v := ev.get("policyViolationEvent"):
            self.violations.append(str(v.get("message", ""))[:200])
        elif ev.get("policyLoadEvent") is not None:
            self.policies += 1
        elif summary := ev.get("policyAnalyzeSummaryEvent"):
            self.policy_resources += 1
            self.policy_checks += len(summary.get("passed") or [])
        elif s := ev.get("summaryEvent"):
            self.summary = {k: v for k, v in (s.get("resourceChanges") or {}).items()}

    def settle(self) -> None:
        """A run that exited cleanly finished everything it started, whatever events were lost."""
        now = time.monotonic()
        for r in self.resources.values():
            if r.status in ("pending", "working"):
                r.status, r.ended = "done", now

    @property
    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started if self.started else 0.0

    @property
    def counts(self) -> dict[str, int]:
        c = {"pending": 0, "working": 0, "done": 0, "failed": 0}
        for r in self.resources.values():
            c[r.status] += 1
        return c


class Pulumi:
    """Runs pulumi in the repo's infra/pulumi directory and feeds its event log into a `Run`."""

    def __init__(self, cfg: CloudConfig) -> None:
        self.cfg = cfg
        self.env = cfg.prepare()
        self.proc: subprocess.Popen[str] | None = None

    def _pulumi(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["pulumi", *args], cwd=PULUMI_DIR, env=self.env, capture_output=True,
                              text=True, check=check, timeout=120)

    def select_stack(self) -> None:
        self._pulumi("stack", "select", "--create", STACK, "--secrets-provider", "passphrase")

    def start(self, verb: str, run: Run, on_event: Callable[[], None] | None = None) -> None:
        """Start `up` or `destroy` and return at once; `run` fills in as events arrive."""
        assert verb in ("up", "destroy", "preview")
        self.select_stack()
        events = self.cfg.state_dir / f"events-{verb}.ndjson"
        events.unlink(missing_ok=True)
        events.touch()
        args = ["pulumi", verb, "--stack", STACK, "--non-interactive", "--event-log", str(events),
                "--color", "never", *self.cfg.config_flags()]
        if verb != "preview":
            args += ["--yes", "--skip-preview"]
        if verb == "up":
            args += ["--policy-pack", "policy"]
        run.started = time.monotonic()
        self.proc = subprocess.Popen(args, cwd=PULUMI_DIR, env=self.env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)

        def tail() -> None:
            assert self.proc
            exited = False
            with open(events) as f:
                while True:
                    line = f.readline()
                    if line:
                        try:
                            run.handle(json.loads(line))
                        except (json.JSONDecodeError, KeyError, ValueError):
                            continue
                        if on_event:
                            on_event()
                    elif exited:
                        break  # one more pass after the exit found nothing left
                    elif self.proc.poll() is not None:
                        exited = True  # events written just before exit are read on the next pass
                        time.sleep(0.2)
                    else:
                        time.sleep(0.05)
            run.exit, run.ended = self.proc.returncode, time.monotonic()
            if run.exit == 0:
                run.settle()
            err = (self.proc.stderr.read() if self.proc.stderr else "").strip()
            if err and run.exit:
                run.log.extend(err.splitlines()[-8:])

        threading.Thread(target=tail, daemon=True).start()

    def wait(self, run: Run) -> int:
        while run.exit is None:
            time.sleep(0.2)
        return run.exit

    def deployed(self) -> list[Res]:
        """The cloud resources the stack currently holds, so a teardown knows its size before it
        starts."""
        out = self._pulumi("stack", "export", "--stack", STACK, check=False).stdout
        try:
            resources = json.loads(out)["deployment"].get("resources", [])
        except (json.JSONDecodeError, KeyError):
            return []
        found = []
        for r in resources:
            if r["type"].startswith(("pulumi:", "ipo:")):
                continue
            typ, name, parent = split_urn(r["urn"])
            found.append(Res(r["urn"], typ, name, parent, "delete"))
        return found

    def outputs(self) -> dict[str, Any]:
        out = self._pulumi("stack", "output", "--stack", STACK, "--json", check=False).stdout
        try:
            return dict(json.loads(out))
        except json.JSONDecodeError:
            return {}


# ------------------------------------------------------------------ the real cloud


@dataclass
class Server:
    id: str
    name: str
    status: str
    flavor: str
    addresses: list[str]
    floating: str = ""


@dataclass
class Snapshot:
    at: float
    project: str = ""
    region: str = ""
    servers: list[Server] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    floating_ips: list[tuple[str, str]] = field(default_factory=list)  # (address, attached to)
    quota: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (used, limit)
    networks: set[str] = field(default_factory=set)  # names that exist: edge-net, r-edge, ...
    error: str = ""


class Probe:
    """Asks the OpenStack APIs what is really there, so the screen shows the cloud, not Pulumi."""

    def __init__(self, cfg: CloudConfig) -> None:
        import openstack

        os.environ["OS_CLIENT_CONFIG_FILE"] = str(cfg.clouds_file)
        self.conn = openstack.connect(cloud=cfg.cloud)
        self.cfg = cfg

    def can_allocate_floating_ip(self) -> tuple[bool, str]:
        """Allocates and releases one floating IP, which is the only way to know the external
        network still has an address for us (its availability API is admin-only)."""
        c = self.conn
        ext = next((n for n in c.network.networks() if n.is_router_external and
                    n.name == "INTERNET"), None) or next(n for n in c.network.networks()
                                                       if n.is_router_external)
        try:
            ip = c.network.create_ip(floating_network_id=ext.id, description="ipo preflight")
        except Exception as e:  # noqa: BLE001
            return False, str(e).split(", ")[-1][:120] or type(e).__name__
        c.network.delete_ip(ip)
        return True, ext.name

    def snapshot(self) -> Snapshot:
        c = self.conn
        snap = Snapshot(time.monotonic(), region=self.cfg.region)
        try:
            snap.project = c.current_project.name
            flavors = {f.id: f.name for f in c.compute.flavors()}
            ports = {p.id: p for p in c.network.ports()}
            fips = list(c.network.ips())
            for s in c.compute.servers(details=True):
                if s.status == "DELETED":
                    continue
                addrs = [a["addr"] for lst in (s.addresses or {}).values() for a in lst
                         if a.get("OS-EXT-IPS:type") != "floating"]
                float_ip = next((a["addr"] for lst in (s.addresses or {}).values() for a in lst
                                 if a.get("OS-EXT-IPS:type") == "floating"), "")
                fl = s.flavor or {}
                snap.servers.append(Server(s.id, s.name, s.status, flavors.get(
                    fl.get("id", ""), fl.get("original_name", "")), addrs, float_ip))
            snap.servers.sort(key=lambda s: s.name)
            snap.floating_ips = [
                (f.floating_ip_address, ports[f.port_id].name if f.port_id in ports else "-") for f in fips]
            snap.networks = ({n.name for n in c.network.networks()}
                             | {r.name for r in c.network.routers()})
            snap.counts = {
                "servers": len(snap.servers),
                "networks": sum(1 for n in c.network.networks() if not n.is_router_external
                                and n.name.endswith("-net")),
                "routers": sum(1 for r in c.network.routers() if r.name.startswith("r-")),
                "ports": len(ports), "security groups": sum(
                    1 for g in c.network.security_groups() if g.name.startswith("sg-")),
                "floating ips": len(fips),
            }
            lim = c.compute.get_limits().absolute
            snap.quota["instances"] = (lim.total_instances_used, lim.max_total_instances)
            snap.quota["cores"] = (lim.total_cores_used, lim.max_total_cores)
            snap.quota["ram GB"] = (lim.total_ram_used // 1024, lim.max_total_ram_size // 1024)
        except Exception as e:  # noqa: BLE001 - a flaky API call must not stop the display
            snap.error = f"{type(e).__name__}: {str(e)[:100]}"
        return snap


# ------------------------------------------------------------------ plain command


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("verb", choices=["up", "destroy", "status"])
    ap.add_argument("--clouds", required=True, type=Path)
    ap.add_argument("--cloud", default="")
    args = ap.parse_args(argv)
    cfg = CloudConfig(args.clouds, args.cloud)
    probe = Probe(cfg)
    if args.verb == "up":
        cfg.floating_ips, why = probe.can_allocate_floating_ip()
        print(f"floating ips: {'available' if cfg.floating_ips else 'NOT available'} ({why})")
    if args.verb == "status":
        s = probe.snapshot()
        print(s.project, s.counts, s.quota, s.error)
        for sv in s.servers:
            print(f"  {sv.name:10} {sv.status:8} {sv.flavor:16} {' '.join(sv.addresses)} {sv.floating}")
        print("  floating:", s.floating_ips)
        return 0
    pulumi = Pulumi(cfg)
    run = Run(args.verb)
    seen: set[tuple[str, str]] = set()

    def show() -> None:
        for r in list(run.resources.values()):
            key = (r.urn, r.status)
            if key not in seen:
                seen.add(key)
                print(f"{run.elapsed:6.1f}s {r.status:8} {r.op:8} {r.parent or '-':14} {r.kind:20} {r.name}",
                      flush=True)

    pulumi.start(args.verb, run, show)
    code = pulumi.wait(run)
    time.sleep(0.5)
    show()
    print(f"\nexit {code} after {run.elapsed:.0f}s; {run.counts}; policies loaded {run.policies}; "
          f"violations {run.violations}; summary {run.summary}")
    for line in run.errors[:6] or run.log[-6:]:
        print("  !", line)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

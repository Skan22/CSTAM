"""The parts of the platform the lab runs for real: gateways (keepalived, Traefik and the agent),
a sandbox backend and the control plane."""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab import netns, render, tools

REPO = tools.REPO
ADMIN = ("lab-admin@felcloud.test", "lab-admin-password")
JWT_SECRET = "lab-jwt-secret-that-is-at-least-forty-characters-long"
MGMT_CLIENT = "10.30.0.254"
EDGE_CLIENT = "10.0.0.254"


def signing_key() -> tuple[str, str]:
    """A fresh Ed25519 key as (base64 seed, base64 public key), the formats the platform uses."""
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(seed).decode(), base64.b64encode(pub).decode()


def http_json(method: str, url: str, *, token: str | None = None, body: Any = None,
              headers: dict[str, str] | None = None, timeout: float = 5) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {}),
        **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def wait_for(cond: Any, what: str, timeout: float = 30, every: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            got = cond()
            if got:
                return got
        except (OSError, urllib.error.URLError, RuntimeError, KeyError) as e:
            last = e
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}" + (f" ({last})" if last else ""))


# ------------------------------------------------------------------ postgres
@dataclass
class Postgres:
    """A private cluster on a unix socket. Start it OUTSIDE the lab: Postgres refuses uid 0."""

    root: Path
    sock: Path = field(init=False)

    def __post_init__(self) -> None:
        # A unix socket path is limited to about 100 bytes, so it cannot live under a deep root.
        self.sock = Path(tempfile.mkdtemp(prefix="ipo-pg-"))

    @staticmethod
    def _bin(name: str) -> str:
        found = shutil.which(name)
        for d in ([found] if found else []) + [f"/usr/bin/{name}", f"/usr/lib/postgresql/16/bin/{name}"]:
            if d and Path(d).exists():
                return d
        raise RuntimeError(f"{name} not found")

    def start(self) -> None:
        data = self.root / "data"
        self.sock.mkdir(parents=True, exist_ok=True)
        subprocess.run([self._bin("initdb"), "-D", str(data), "-U", "ipo", "--auth=trust",
                        "-E", "UTF8"], check=True, capture_output=True)
        opts = (f"-c listen_addresses='' -c unix_socket_directories={self.sock} -c fsync=off "
                "-c synchronous_commit=off -c full_page_writes=off -c max_connections=100")
        subprocess.run([self._bin("pg_ctl"), "-D", str(data), "-o", opts, "-w", "-l",
                        str(self.root / "pg.log"), "start"], check=True, capture_output=True)
        subprocess.run([self._bin("createdb"), "-h", str(self.sock), "-U", "ipo", "ipo"],
                       check=True, capture_output=True)

    def stop(self) -> None:
        subprocess.run([self._bin("pg_ctl"), "-D", str(self.root / "data"), "-m", "immediate",
                        "stop"], capture_output=True)
        shutil.rmtree(self.sock, ignore_errors=True)

    @property
    def dsn(self) -> str:
        return f"postgresql://ipo@/ipo?host={self.sock}"


# ------------------------------------------------------------------- backend
class Backend:
    """Answers on every sandbox pool address, from its own namespace on sandbox-net."""

    NS = "bk"

    def __init__(self, lab: netns.Lab, plat: dict[str, Any]) -> None:
        self.lab, self.plat = lab, plat

    def start(self) -> None:
        import ipaddress
        pool = self.plat["ip_pool"]
        self.lab.namespace(self.NS)
        prefix = pool["cidr"].split("/")[1]
        first, last = (int(ipaddress.IPv4Address(pool[k])) for k in ("first", "last"))
        self.lab.attach(self.NS, "sbx", "sbx0", f"{ipaddress.IPv4Address(first)}/{prefix}")
        for i in range(first + 1, last + 1):
            netns.run(["ip", "addr", "add", f"{ipaddress.IPv4Address(i)}/{prefix}", "dev", "sbx0"],
                      ns=self.NS)
        self.proc = self.lab.spawn(self.NS, [sys.executable, str(Path(__file__).with_name("backend.py")),
                                             "80"], self.lab.workdir / "backend.log")


# ------------------------------------------------------------------- gateway
class Gateway:
    """One gateway VM: three NICs, keepalived, Traefik and ipo-agent, all real."""

    def __init__(self, lab: netns.Lab, plat: dict[str, Any], name: str, *, cp_url: str,
                 signing_public: str) -> None:
        self.lab, self.plat, self.name = lab, plat, name
        self.ns = name.replace("-", "")
        self.addr = plat["network"]["hosts"][name]
        self.root = lab.workdir / name
        self.run_dir, self.state_dir, self.log_dir, self.etc = (
            self.root / d for d in ("run", "state", "log", "etc"))
        self.bin_dir = self.root / "bin"
        self.cp_url, self.signing_public = cp_url, signing_public
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.off = False
        # address key -> (bridge, interface, subnet)
        self.nics = {"edge": ("edge", "edge0", plat["network"]["edge_cidr"]),
                     "sandbox": ("sbx", "sbx0", plat["ip_pool"]["cidr"]),
                     "mgmt": ("mgmt", "mgmt0", plat["network"]["mgmt_cidr"])}
        for d in (self.run_dir, self.state_dir, self.log_dir, self.etc, self.bin_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- files
    @property
    def state_file(self) -> Path:
        return self.run_dir / "vrrp_state"

    @property
    def fault_file(self) -> Path:
        return self.run_dir / "fault"

    @property
    def access_log(self) -> Path:
        return self.log_dir / "access.log"

    @property
    def agent_url(self) -> str:
        return f"http://{self.addr['mgmt']}:8443"

    def render(self) -> None:
        for f in (REPO / "ansible/roles/keepalived/files").iterdir():
            shutil.copy(f, self.bin_dir / f.name)
            (self.bin_dir / f.name).chmod(0o755)
        (self.etc / "keepalived.conf").write_text(render.render(
            "keepalived", "keepalived.conf.j2",
            **render.keepalived_vars(self.plat, self.name, interface="edge0", bin_dir=self.bin_dir,
                                     state_file=self.state_file, fault_file=self.fault_file)))
        (self.etc / "traefik.yml").write_text(render.render(
            "traefik", "traefik.yml.j2",
            **render.traefik_vars(web_port=80, api_address="127.0.0.1:8080",
                                  access_log=self.access_log, live=self.state_dir / "live.yml")))

    # -- machine
    def plug(self) -> None:
        self.lab.namespace(self.ns)
        for key, (bridge, ifname, cidr) in self.nics.items():
            self.lab.attach(self.ns, bridge, ifname, f"{self.addr[key]}/{cidr.split('/')[1]}")

    def power_off(self) -> None:
        """A hard reset: every process dies at once and the NICs go dark, taking the VIP with them."""
        self.kill_all()
        self.off = True
        for _, ifname, _ in self.nics.values():
            netns.run(["ip", "link", "set", ifname, "down"], ns=self.ns)
            netns.run(["ip", "addr", "flush", "dev", ifname], ns=self.ns)

    def power_on(self) -> None:
        """Boot: the NICs come back with only their own addresses, and the services start."""
        for key, (_, ifname, cidr) in self.nics.items():
            netns.run(["ip", "addr", "flush", "dev", ifname], ns=self.ns)
            netns.run(["ip", "addr", "add", f"{self.addr[key]}/{cidr.split('/')[1]}",
                       "dev", ifname], ns=self.ns)
            netns.run(["ip", "link", "set", ifname, "up"], ns=self.ns)
        for f in (self.state_file, self.fault_file):
            f.unlink(missing_ok=True)  # /run is a tmpfs
        self.off = False
        self.start()

    def repair(self) -> None:
        """Bring back whatever a scenario took down: the machine, then any dead service."""
        if self.off:
            self.power_on()
            return
        self.fault_file.unlink(missing_ok=True)
        for name, start in (("agent", self.start_agent), ("traefik", self.start_traefik),
                            ("keepalived", self.start_keepalived)):
            p = self.procs.get(name)
            if p is None or p.poll() is not None:
                start()

    # -- services
    def _spawn(self, name: str, argv: list[str], env: dict[str, str] | None = None) -> None:
        self.procs[name] = self.lab.spawn(self.ns, argv, self.log_dir / f"{name}.log", env)

    def start_traefik(self) -> None:
        binary = tools.traefik()
        assert binary
        self._spawn("traefik", [str(binary), f"--configFile={self.etc / 'traefik.yml'}"])

    def start_agent(self) -> None:
        binary = tools.agent()
        assert binary
        self._spawn("agent", [str(binary)], {
            "IPO_AGENT_NAME": self.name, "IPO_SIGNING_PUBLIC_KEY": self.signing_public,
            "IPO_SANDBOX_CIDR": self.plat["ip_pool"]["cidr"], "IPO_CONFIG_DIR": str(self.state_dir),
            "IPO_VRRP_STATE_FILE": str(self.state_file), "IPO_VRRP_FAULT_FILE": str(self.fault_file),
            "IPO_AGENT_LISTEN": f"{self.addr['mgmt']}:8443", "IPO_AGENT_INSECURE": "1",
            "IPO_METRICS_ADDR": "127.0.0.1:9100", "IPO_HEARTBEAT_SECONDS": "1",
            "IPO_PULL_SECONDS": "1", "IPO_CP_URL": self.cp_url,
            "IPO_CP_EMAIL": ADMIN[0], "IPO_CP_PASSWORD": ADMIN[1]})

    def start_keepalived(self) -> None:
        binary = tools.keepalived()
        assert binary
        shim = tools.setgroups_shim()
        assert shim, "the lab needs cc to build lab/nosetgroups.c"
        self._spawn("keepalived", [
            str(binary), "--dont-fork", "--log-console", "--log-detail", "--vrrp",
            "-f", str(self.etc / "keepalived.conf"), "--pid", str(self.run_dir / "keepalived.pid"),
            "--vrrp_pid", str(self.run_dir / "vrrp.pid")], {"LD_PRELOAD": str(shim)})

    def start(self) -> None:
        # Traefik's file provider gives up for good if the file is missing at its start, so the
        # agent (which creates it) goes first; the traefik unit orders itself the same way.
        self.start_agent()
        wait_for(lambda: (self.state_dir / "live.yml").exists(), f"{self.name} live.yml", 10, 0.05)
        self.start_traefik()
        wait_for(self.traefik_up, f"{self.name} traefik", 15)
        self.start_keepalived()

    def traefik_up(self) -> bool:
        p = netns.run(["curl", "-sf", "--max-time", "1", "http://127.0.0.1:8080/ping"],
                      ns=self.ns, check=False)
        return p.returncode == 0

    def signal(self, name: str, sig: int) -> None:
        p = self.procs.get(name)
        if p and p.poll() is None:
            os.killpg(p.pid, sig)
            if sig in (signal.SIGKILL, signal.SIGTERM):
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait(5)

    def kill_all(self) -> None:
        for name in list(self.procs):
            self.signal(name, signal.SIGKILL)

    # -- observation
    def vrrp_state(self) -> str:
        try:
            return self.state_file.read_text().strip() or "UNKNOWN"
        except FileNotFoundError:
            return "UNKNOWN"

    def has_vip(self) -> bool:
        out = netns.run(["ip", "-4", "-o", "addr", "show", "dev", "edge0"], ns=self.ns, check=False)
        return f"{self.plat['network']['vip']}/" in out.stdout

    def status(self) -> dict[str, Any]:
        code, body = http_json("GET", f"{self.agent_url}/status", timeout=2)
        if code != 200:
            raise RuntimeError(f"{self.name} agent answered {code}")
        return dict(body)


# -------------------------------------------------------------- control plane
class ControlPlane:
    """The real control plane with a fake cloud, pushing to the real agents."""

    def __init__(self, lab: netns.Lab, plat: dict[str, Any], dsn: str, seed: str) -> None:
        self.lab, self.dsn, self.seed = lab, dsn, seed
        self.url = f"http://{MGMT_CLIENT}:8000"
        self.token = ""
        self.plat = plat

    def start(self) -> None:
        py = REPO / "control-plane" / ".venv" / "bin" / "python"
        env = {
            "IPO_DATABASE_URL": self.dsn, "IPO_PLATFORM": str(REPO / "platform.yaml"),
            "IPO_JWT_SECRET": JWT_SECRET, "IPO_CLOUD": "fake", "IPO_SIGNING_KEY": self.seed,
            "IPO_MIGRATE": "1", "IPO_ADMIN_EMAIL": ADMIN[0], "IPO_ADMIN_PASSWORD": ADMIN[1],
            "IPO_BIND": f"{MGMT_CLIENT}:8000", "IPO_VIP": self.plat["network"]["vip"],
            "IPO_AGENTS": ",".join(f"{g}=http://{self.plat['network']['hosts'][g]['mgmt']}:8443"
                                   for g in ("gw-a", "gw-b")),
        }
        self.proc = self.lab.spawn(None, [str(py), "-m", "ipo.main"], self.lab.workdir / "cp.log", env)
        wait_for(lambda: http_json("GET", f"{self.url}/healthz")[0] == 200, "control plane", 60, 0.3)
        code, body = http_json("POST", f"{self.url}/v1/auth/login",
                               body={"email": ADMIN[0], "password": ADMIN[1]})
        assert code == 200, body
        self.token = body["access_token"]

    def stop(self) -> None:
        os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)

    def api(self, method: str, path: str, body: Any = None, **kw: Any) -> tuple[int, Any]:
        return http_json(method, self.url + path, token=self.token, body=body, **kw)

    def split_brain(self) -> bool:
        return bool(self.api("GET", "/v1/gateways")[1]["split_brain"])

    def register(self, slug: str) -> dict[str, Any]:
        code, body = self.api("POST", "/v1/teams", {"slug": slug},
                              headers={"Idempotency-Key": f"lab-{slug}"})
        assert code == 202, (code, body)
        return dict(body)

    def wait_active(self, slug: str, timeout: float = 60) -> dict[str, Any]:
        def active() -> dict[str, Any] | None:
            code, teams = self.api("GET", "/v1/teams")
            for t in teams if code == 200 else []:
                if t["slug"] == slug and t["state"] == "active":
                    return dict(t)
            return None
        return dict(wait_for(active, f"team {slug} to become active", timeout, 0.3))

    def metric(self, name: str, labels: str = "") -> float | None:
        req = urllib.request.Request(f"{self.url}/metrics")
        with urllib.request.urlopen(req, timeout=3) as r:
            for line in r.read().decode().splitlines():
                if line.startswith(name + labels) and not line.startswith("#"):
                    return float(line.rsplit(" ", 1)[1])
        return None

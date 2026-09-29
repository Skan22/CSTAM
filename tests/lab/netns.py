"""Network-namespace labs that run without root.

Everything here runs inside `unshare --user --map-root-user --net --mount`: the caller is uid 0 in
a private user, network and mount namespace, so it can create bridges, veth pairs, namespaces and
nftables rules that exist only for this process tree and vanish when it exits.
"""

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

NETNS_DIR = "/var/run/netns"

UNSHARE = ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork",
           "--kill-child", "--mount-proc"]


def in_lab(argv: Sequence[str]) -> list[str]:
    """The command line that runs `argv` inside a fresh lab namespace."""
    return [*UNSHARE, *argv]


def run(cmd: Sequence[str], *, check: bool = True, ns: str | None = None,
        input: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    full = [*nsenter(ns), *cmd] if ns else list(cmd)
    p = subprocess.run(full, capture_output=True, text=True, input=input, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(full)} failed ({p.returncode}): {p.stderr.strip()}")
    return p


def nsenter(ns: str) -> list[str]:
    return ["nsenter", f"--net={NETNS_DIR}/{ns}", "--"]


@dataclass
class Nic:
    ns: str
    name: str
    bridge: str
    addr: str


@dataclass
class Lab:
    workdir: Path
    bridges: dict[str, str] = field(default_factory=dict)
    namespaces: list[str] = field(default_factory=list)
    _veth: int = 0

    def setup(self) -> None:
        if os.geteuid() != 0:
            raise RuntimeError("run inside `unshare -Urnm` (see lab.netns.in_lab)")
        self.workdir.mkdir(parents=True, exist_ok=True)
        run(["mount", "-t", "tmpfs", "tmpfs", "/var/run"])
        Path(NETNS_DIR).mkdir(parents=True, exist_ok=True)
        run(["ip", "link", "set", "lo", "up"])

    def bridge(self, name: str, addr: str | None = None) -> None:
        run(["ip", "link", "add", "name", name, "type", "bridge", "stp_state", "0"])
        run(["ip", "link", "set", name, "up"])
        if addr:
            run(["ip", "addr", "add", addr, "dev", name])
        self.bridges[name] = addr or ""

    def namespace(self, name: str) -> None:
        run(["ip", "netns", "add", name])
        run(["ip", "link", "set", "lo", "up"], ns=name)
        run(["sysctl", "-qw", "net.ipv4.ip_forward=0"], ns=name, check=False)
        self.namespaces.append(name)

    def attach(self, ns: str, bridge: str, ifname: str, addr: str | None,
               *, mac: str | None = None) -> str:
        """Connect `ns` to `bridge` with a veth pair; returns the bridge-side port name."""
        self._veth += 1
        port, tmp = f"p{self._veth}", f"t{self._veth}"
        run(["ip", "link", "add", "name", port, "type", "veth", "peer", "name", tmp])
        run(["ip", "link", "set", tmp, "netns", ns])
        run(["ip", "link", "set", tmp, "name", ifname], ns=ns)
        if mac:
            run(["ip", "link", "set", ifname, "address", mac], ns=ns)
        run(["ip", "link", "set", port, "master", bridge, "up"])
        if addr:
            run(["ip", "addr", "add", addr, "dev", ifname], ns=ns)
        run(["ip", "link", "set", ifname, "up"], ns=ns)
        return port

    def spawn(self, ns: str | None, argv: Sequence[str], log: Path,
              env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
        """Start a long-running process (in its own session so it can be killed as a group)."""
        full = [*nsenter(ns), *argv] if ns else list(argv)
        out = open(log, "ab")  # noqa: SIM115 - owned by the child for its whole life
        return subprocess.Popen(full, stdout=out, stderr=subprocess.STDOUT,
                                env={**os.environ, **(env or {})}, start_new_session=True)

"""The platform's networks as Linux namespaces, with both enforcement layers switchable.

    internet ── br-ext ──┬── r-edge ──┬── br-edge ───── gw-a, gw-b (edge ports, VIP on gw-a)
                         │            └── br-sandbox ── sbx-a, sbx-b, relay, gw-a, gw-b
                         └── r-mgmt ───── br-mgmt ───── gw-a, gw-b, cp-1, cp-2, relay, bastion

The routers stand in for Neutron routers: they forward anything between the networks they join
and map the floating IPs (VIP and bastion) with DNAT. They are not firewalls, so a machine that
routes itself somewhere it should not go is stopped only by the two layers under test:

- the host layer: each platform host (not the sandboxes, which the teams control) loads the
  nftables file the `hardening` role renders, with the role's sysctls;
- the fabric: a bridge-family ruleset in the lab's root namespace that behaves like Neutron with
  port security on, i.e. a port may only send its own MAC and addresses (checked before the bridge
  learns the MAC, as OVS does) and each port admits only its security group's ingress rules,
  statefully, without looking at the destination address.

Both are rendered from the same resolved rules (`ipo_net.ingress`) by different code; the oracle
in `model` reads the spec without that resolver, so a resolver bug shows up as a mismatch.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from isolation import model
from isolation.model import ipo_net
from lab import netns, render

HERE = Path(__file__).parent
BRIDGES = {"ext": "br-ext", "edge": "br-edge", "sandbox": "br-sandbox", "mgmt": "br-mgmt"}
ROUTERS = {"r-edge": ("ext", "edge", "sandbox"), "r-mgmt": ("ext", "mgmt")}
REACHABLE = ("open", "refused", "one-way")
EDGE_TABLE = 100
NO_IPV6 = (["sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1"],
           ["sysctl", "-qw", "net.ipv6.conf.default.disable_ipv6=1"])


@dataclass(frozen=True)
class Port:
    machine: str
    net: str
    name: str  # the bridge side of the veth
    mac: str
    ip: str
    kind: str  # host, sandbox, router or outside


@dataclass(frozen=True)
class Result:
    probe: model.Probe
    verdict: str

    @property
    def reachable(self) -> bool:
        return self.verdict in REACHABLE

    def __str__(self) -> str:
        return f"{self.probe.name}: {self.verdict}"


class IsolationLab:
    def __init__(self, workdir: Path, platform: dict[str, Any], spec: dict[str, Any]) -> None:
        self.workdir, self.platform, self.spec = workdir, platform, spec
        self.nodes = model.topology(platform, spec)
        self.base = netns.Lab(workdir)
        self.ports: list[Port] = []
        self.procs: list[subprocess.Popen[bytes]] = []
        self.logs: dict[str, Path] = {}

    # ------------------------------------------------------------------ shape

    @property
    def machines(self) -> list[str]:
        return [n for n in self.nodes if n != "vip"]

    @property
    def platform_hosts(self) -> list[str]:
        return list(self.platform["network"]["hosts"])

    def cidr(self, net: str) -> str:
        n = self.platform["network"]
        return str({"ext": f"{model.EXTERNAL}.0/24", "edge": n["edge_cidr"], "mgmt": n["mgmt_cidr"],
                    "sandbox": self.platform["ip_pool"]["cidr"]}[net])

    def router_addr(self, router: str, net: str) -> str:
        if net == "ext":
            return f"{model.EXTERNAL}.{2 if router == 'r-edge' else 3}"
        return str(ipaddress.ip_network(self.cidr(net))[1])

    def port(self, machine: str, net: str) -> Port:
        return next(p for p in self.ports if p.machine == machine and p.net == net)

    # ------------------------------------------------------------------ build

    def build(self) -> None:
        self.base.setup()
        for cmd in NO_IPV6:
            netns.run(cmd)
        for br in BRIDGES.values():
            self.base.bridge(br)
        for name in [*self.machines, *ROUTERS]:
            self.base.namespace(name)
            for cmd in NO_IPV6:
                netns.run(cmd, ns=name)
        for name in self.machines:
            group = self.nodes[name].group
            kind = {"internet": "outside", "sg-sandbox": "sandbox"}.get(group, "host")
            for net, addr in self.nodes[name].addrs.items():
                self._plug(name, net, addr, kind)
        for router, nets in ROUTERS.items():
            for net in nets:
                self._plug(router, net, self.router_addr(router, net), "router")
            netns.run(["sysctl", "-qw", "net.ipv4.ip_forward=1"], ns=router)
        self._routes()
        self._floating_ips()
        for host in self.platform_hosts:
            self._sysctls(host)
        self._listeners()

    def _plug(self, machine: str, net: str, addr: str, kind: str) -> None:
        n = len(self.ports) + 1
        mac = f"02:00:00:00:{n >> 8:02x}:{n & 0xFF:02x}"
        prefix = self.cidr(net).split("/")[1]
        name = self.base.attach(machine, BRIDGES[net], net, f"{addr}/{prefix}", mac=mac)
        self.ports.append(Port(machine, net, name, mac, addr, kind))

    def _routes(self) -> None:
        edge_gw = self.router_addr("r-edge", "edge")
        for gw in self.platform_hosts:
            if self.nodes[gw].group == "sg-gateway":
                # The edge port is the gateway's first NIC, so its default route is the edge one.
                netns.run(["ip", "route", "add", "default", "via", edge_gw, "dev", "edge"], ns=gw)
                self.edge_routing(gw, on=True)
            else:
                netns.run(["ip", "route", "add", "default", "via", self.router_addr("r-mgmt", "mgmt"),
                           "dev", "mgmt"], ns=gw)
        for name, node in self.nodes.items():
            if node.group == "sg-sandbox":
                self.restore_routes(name)
        vip = self.platform["network"]["vip"]
        netns.run(["ip", "addr", "add", f"{vip}/32", "dev", "edge"], ns="gw-a")

    def edge_routing(self, gw: str, *, on: bool) -> None:
        """Start or stop the `gateway_network` role's unit on a gateway."""
        unit = render.render(
            "gateway_network", "ipo-edge-routing.service.j2",
            gateway_network_edge_cidr=self.cidr("edge"),
            gateway_network_edge_router=self.router_addr("r-edge", "edge"),
            gateway_network_edge_interface="edge", gateway_network_table=EDGE_TABLE,
            gateway_network_ip="ip")
        if on:
            self._run_unit(gw, unit)
        else:
            for line in unit.splitlines():
                if line.startswith("ExecStop="):
                    netns.run(line.removeprefix("ExecStop=").lstrip("-").split(), ns=gw, check=False)

    @staticmethod
    def _run_unit(ns: str, unit: str) -> None:
        """Run a rendered systemd unit's ExecStart lines in order, as systemd would on boot."""
        for line in unit.splitlines():
            if line.startswith("ExecStart="):
                cmd = line.removeprefix("ExecStart=")
                netns.run(cmd.lstrip("-").split(), ns=ns, check=not cmd.startswith("-"))

    def _floating_ips(self) -> None:
        for router, fip, inside in (
                ("r-edge", model.FLOATING_IP, self.platform["network"]["vip"]),
                ("r-mgmt", model.BASTION_FIP, self.nodes["bastion"].addrs["mgmt"])):
            netns.run(["ip", "addr", "add", f"{fip}/32", "dev", "ext"], ns=router)
            netns.run(["nft", "-f", "-"], ns=router, input=(
                "table ip nat {\n  chain prerouting {\n"
                "    type nat hook prerouting priority dstnat; policy accept;\n"
                f"    ip daddr {fip} dnat to {inside}\n  }}\n}}\n"))

    def _sysctls(self, host: str) -> None:
        text = render.render("hardening", "sysctl.conf.j2", **render.role_vars(
            "hardening", host, plat=self.platform, spec=self.spec))
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                key, value = (x.strip() for x in line.split("=", 1))
                netns.run(["sysctl", "-qw", f"{key}={value}"], ns=host)

    def _listeners(self) -> None:
        tcp, udp = ",".join(map(str, model.TCP_PORTS)), ",".join(map(str, model.UDP_PORTS))
        for name in self.machines:
            if name == "internet":
                continue
            raw = "112" if self.nodes[name].group == "sg-gateway" else ""
            self.logs[name] = log = self.workdir / f"received-{name}.log"
            log.write_text("")
            out = self.workdir / f"listener-{name}.out"
            self.procs.append(self.base.spawn(
                name, [sys.executable, str(HERE / "listener.py"), str(log), tcp, udp, raw], out))
        outs = [self.workdir / f"listener-{n}.out" for n in self.logs]
        deadline = time.monotonic() + 20
        while not all(o.exists() and o.read_text().startswith("ready") for o in outs):
            if time.monotonic() > deadline:
                raise RuntimeError(f"listeners did not start; see {self.workdir}")
            time.sleep(0.1)

    def close(self) -> None:
        for p in self.procs:
            p.kill()

    # ------------------------------------------------------------------ layers

    def host_ruleset(self, host: str, spec: dict[str, Any]) -> str:
        """What the `hardening` role writes to this host, from its defaults and group_vars. The
        include of /etc/nftables.d is dropped: in the lab it would read this machine's files."""
        text = render.render("hardening", "nftables.conf.j2", **render.role_vars(
            "hardening", host, plat=self.platform, spec=spec))
        return "".join(line for line in text.splitlines(keepends=True)
                       if not line.startswith("include "))

    def fabric_ruleset(self, spec: dict[str, Any]) -> str:
        vip = self.platform["network"]["vip"]
        sec = ["table bridge ipo_fabric {", "  chain port_security {",
               "    type filter hook prerouting priority -300; policy accept;"]
        gateways = {m for m, n in self.nodes.items() if n.group == "sg-gateway" and m != "vip"}
        for p in self.ports:
            if p.kind == "outside":
                continue
            sec.append(f'    iifname "{p.name}" ether saddr != {p.mac} drop')
            sec.append(f'    iifname "{p.name}" ether type arp arp saddr ether != {p.mac} drop')
            if p.kind != "router":  # routers forward other machines' packets
                own = [p.ip, *([vip] if p.machine in gateways and p.net == "edge" else [])]
                ips = ", ".join(own)  # the VIP is an allowed address pair on both edge ports
                sec.append(f'    iifname "{p.name}" ether type ip ip saddr != {{ {ips} }} drop')
                sec.append(f'    iifname "{p.name}" ether type arp arp saddr ip != {{ {ips} }} drop')
        sec += ["  }", "  chain security_groups {",
                "    type filter hook forward priority 0; policy accept;",
                "    ct state established,related accept", "    ether type arp accept"]
        for p in self.ports:
            if p.kind in ("router", "outside"):
                continue
            for r in ipo_net.ingress(spec, self.platform, self.nodes[p.machine].group):
                if r["network"] != p.net:
                    continue
                src = "" if r["sources"] == [ipo_net.ANYWHERE] else (
                    f"ip saddr {{ {', '.join(r['sources'])} }}")
                what = {"vrrp": "ip protocol vrrp", "any": ""}.get(
                    r["proto"], f"{r['proto']} dport {{ {', '.join(map(str, r['ports']))} }}")
                parts = [f'oifname "{p.name}" ether type ip', src, what, "accept"]
                sec.append("    " + " ".join(x for x in parts if x))
            sec.append(f'    oifname "{p.name}" drop')
        sec += ["  }", "}"]
        return "\n".join(sec) + "\n"

    def set_layers(self, *, host: bool, fabric: bool, host_spec: dict[str, Any] | None = None,
                   fabric_spec: dict[str, Any] | None = None) -> None:
        for h in self.platform_hosts:
            netns.run(["nft", "flush", "ruleset"], ns=h)
            if host:
                netns.run(["nft", "-f", "-"], ns=h, input=self.host_ruleset(h, host_spec or self.spec))
        netns.run(["nft", "flush", "ruleset"])
        if fabric:
            netns.run(["nft", "-f", "-"], input=self.fabric_ruleset(fabric_spec or self.spec))
        self.forget()

    def forget(self) -> None:
        """Clear connection tracking and MAC learning, so nothing a previous pass did carries over."""
        flush = [sys.executable, str(HERE / "ctflush.py")]
        netns.run(flush)
        for ns in [*self.machines, *ROUTERS]:
            netns.run(flush, ns=ns)
        for br in BRIDGES.values():
            netns.run(["ip", "link", "set", "dev", br, "type", "bridge", "fdb_flush"])

    # ------------------------------------------------------------------ routes

    def reroute(self, machine: str, nets: list[str], via: str) -> None:
        for net in nets:
            netns.run(["ip", "route", "replace", self.cidr(net), "via", via], ns=machine)

    def restore_routes(self, machine: str) -> None:
        """A sandbox's routes as Neutron's DHCP would give them; the internet has none inside."""
        for net in ("edge", "sandbox", "mgmt"):
            if net not in self.nodes[machine].addrs:
                netns.run(["ip", "route", "del", self.cidr(net)], ns=machine, check=False)
        if self.nodes[machine].group == "sg-sandbox":
            netns.run(["ip", "route", "replace", "default", "via",
                       self.router_addr("r-edge", "sandbox"), "dev", "sandbox"], ns=machine)

    def neighbours(self, machine: str) -> list[tuple[str, str]]:
        """(owner, address) of everything sharing a network with `machine`: its possible next hops."""
        mine = set(self.nodes[machine].addrs)
        return [(p.machine, p.ip) for p in self.ports
                if p.net in mine and p.machine != machine]

    # ------------------------------------------------------------------ probing

    def probe(self, jobs: dict[str, list[model.Probe]]) -> list[Result]:
        """Run every source's probes at once, each source from inside its own namespace. A UDP
        probe that got no answer but whose datagram a listener logged is `one-way`: delivered."""
        run = uuid.uuid4().hex[:10]

        def one(src: str) -> list[str]:
            if not jobs[src]:
                return []
            req = {"tag": f"{run}:{src}", "probes": [
                {"addr": p.addr, "proto": p.proto, "port": p.port} for p in jobs[src]]}
            out = netns.run([sys.executable, str(HERE / "worker.py")], ns=src, timeout=60,
                            input=json.dumps(req))
            verdicts: list[str] = json.loads(out.stdout)
            return verdicts

        with ThreadPoolExecutor(max(1, len(jobs))) as pool:
            verdicts = dict(zip(jobs, pool.map(one, jobs), strict=True))
        delivered = {tuple(line.payload.split(":")[1:]) for line in self.received()
                     if line.payload.startswith(f"{run}:")}
        out = []
        for src, probes in jobs.items():
            for i, (p, v) in enumerate(zip(probes, verdicts[src], strict=True)):
                if v == "blocked" and (src, str(i)) in delivered:
                    v = "one-way"
                out.append(Result(p, v))
        return out

    def matrix(self, sources: list[str] | None = None, *, spec: dict[str, Any] | None = None,
               attack: bool = False) -> list[Result]:
        spec = spec or self.spec
        sources = sources or [m for m in self.machines]
        return self.probe({s: model.probes_from(self.platform, spec, s, attack=attack)
                           for s in sources})

    # ------------------------------------------------------------------ forged traffic

    def send_frame(self, machine: str, net: str, dst: Port, *, src_mac: str, src_ip: str,
                   dport: int, payload: str) -> None:
        netns.run([sys.executable, str(HERE / "inject.py"), "frame", net, dst.mac, src_mac, src_ip,
                   dst.ip, str(dport), payload], ns=machine)

    def flood(self, machine: str, dst_ip: str, port: int, count: int, rate: int,
              payload: str) -> None:
        netns.run([sys.executable, str(HERE / "inject.py"), "flood", dst_ip, str(port), str(count),
                   str(rate), payload], ns=machine)

    def send_ipproto(self, machine: str, proto: int, dst_ip: str, payload: str) -> None:
        netns.run([sys.executable, str(HERE / "inject.py"), "ipproto", str(proto), dst_ip, payload],
                  ns=machine)

    def received(self, machine: str | None = None) -> list[Received]:
        out = []
        for name, log in self.logs.items():
            if machine and name != machine:
                continue
            for line in log.read_text().splitlines():
                proto, port, dst, src, payload = line.split(" ", 4)
                out.append(Received(name, proto, int(port), dst, src, payload))
        return out

    def delivered(self, tag: str, wait: float = 1.0) -> list[Received]:
        """Log lines whose payload starts with `tag`, after giving stragglers `wait` seconds."""
        time.sleep(wait)
        return [r for r in self.received() if r.payload.startswith(tag)]


@dataclass(frozen=True)
class Received:
    machine: str
    proto: str
    port: int
    dst: str
    src: str
    payload: str

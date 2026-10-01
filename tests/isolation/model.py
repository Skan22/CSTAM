"""What the network is supposed to allow, derived from platform.yaml and security-groups.yaml.

`expected_open` is the oracle. The isolation suite probes every source, destination, address and
port the topology offers and fails wherever the network disagrees with it: a path that is open but
should not be (a leak) or closed but should be open (a broken platform, or a probe that proves
nothing).
"""

from __future__ import annotations

import importlib.util
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
NETS = ("edge", "sandbox", "mgmt")
TCP_PORTS = (22, 80, 443, 3000, 3100, 4317, 4318, 5432, 8000, 8001, 8080, 8443, 9000, 9090, 9100)
UDP_PORTS = (5140, 51820)
EXTERNAL = "203.0.113"  # documentation range: the lab's "internet"
FLOATING_IP = f"{EXTERNAL}.10"  # maps to the VIP
BASTION_FIP = f"{EXTERNAL}.11"  # maps to the bastion
INTERNET_ADDR = f"{EXTERNAL}.50"


def _plugin() -> Any:
    path = REPO / "ansible/filter_plugins/ipo_net.py"
    spec = importlib.util.spec_from_file_location("ipo_net", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ipo_net = _plugin()


def load() -> tuple[dict[str, Any], dict[str, Any]]:
    """(platform, security groups)."""
    return (yaml.safe_load((REPO / "platform.yaml").read_text()),
            yaml.safe_load((REPO / "security-groups.yaml").read_text()))


@dataclass(frozen=True)
class Node:
    name: str
    group: str  # a security group, or "internet"
    addrs: dict[str, str]  # network -> address


def topology(platform: dict[str, Any], spec: dict[str, Any]) -> dict[str, Node]:
    """Every machine the lab runs. `vip` is the shared address, owned by the gateway holding it."""
    nodes = {
        h: Node(h, ipo_net.group_of(spec, h), dict(addrs))
        for h, addrs in platform["network"]["hosts"].items()
    }
    first = ipaddress.IPv4Address(platform["ip_pool"]["first"])
    nodes["sbx-a"] = Node("sbx-a", "sg-sandbox", {"sandbox": str(first)})
    nodes["sbx-b"] = Node("sbx-b", "sg-sandbox", {"sandbox": str(first + 1)})
    nodes["vip"] = Node("vip", "sg-gateway", {"edge": platform["network"]["vip"]})
    nodes["internet"] = Node("internet", "internet", {"ext": INTERNET_ADDR})
    return nodes


def path(src: Node, dst: Node, net: str) -> str | None:
    """How `src` reaches `dst`'s address on `net`, or None when the network has no route at all.
    `direct` shares the segment, `routed` goes through r-edge, `fip` through a floating IP."""
    if net not in dst.addrs:
        return None
    if src.group == "internet":
        # Only the two floating IPs exist from outside.
        return "fip" if dst.name in ("vip", "bastion") else None
    if net in src.addrs:
        return "direct"
    if net == "edge" and src.group == "sg-sandbox":
        return "routed"  # r-edge joins sandbox-net and edge-net
    return None


def admits(spec: dict[str, Any], src_group: str, dst_group: str, net: str, proto: str,
           port: int) -> bool:
    for r in spec["groups"][dst_group]["ingress"]:
        if r["network"] != net or r["proto"] not in (proto, "any"):
            continue
        if r["from"] not in (src_group, "internet"):
            continue
        if r["proto"] == "any" or port in r.get("ports", []):
            return True
    return False


@dataclass(frozen=True)
class Probe:
    src: str
    dst: str
    net: str
    addr: str  # what the source actually dials
    proto: str
    port: int
    expect_open: bool  # the policy admits it and the topology has a path
    policy_open: bool  # the policy admits it whatever the path: the most a rerouting attacker may get
    how: str  # direct, routed, fip, attack (the source routed itself there) or none

    @property
    def name(self) -> str:
        verb = "reaches" if self.expect_open else "is blocked from"
        return f"{self.src} {verb} {self.dst}[{self.net}] {self.addr} {self.proto}/{self.port} ({self.how})"


def expected_open(spec: dict[str, Any], src: Node, dst: Node, net: str, proto: str,
                  port: int) -> bool:
    return path(src, dst, net) is not None and admits(spec, src.group, dst.group, net, proto, port)


def weak_host_open(spec: dict[str, Any], src: Node, dst: Node, net: str, proto: str,
                   port: int) -> bool:
    """Whether a fabric that filters by port, not by destination address (as Neutron's security
    groups do), lets `src` hit `dst`'s address on `net` by routing it via `dst`'s port on a network
    they share: Linux accepts any of its own addresses on any interface."""
    return any(admits(spec, src.group, dst.group, n, proto, port)
               for n in dst.addrs if n != net and n in src.addrs)


def probes_from(platform: dict[str, Any], spec: dict[str, Any], src_name: str,
                *, attack: bool = False) -> list[Probe]:
    """Every probe `src_name` runs: all other machines, all their addresses, all ports, including
    addresses it has no route to (which must stay unreachable). With `attack` the internet dials
    inside addresses directly, as if it had routed itself into the platform."""
    nodes = topology(platform, spec)
    src = nodes[src_name]
    out = []
    for dst in nodes.values():
        if dst.name in (src.name, "internet"):
            continue
        if dst.name == "vip" and src.group == "sg-gateway":
            continue  # a gateway dialling its own VIP tests nothing
        for net, inside in dst.addrs.items():
            how = path(src, dst, net)
            addr = inside
            if src.group == "internet":
                if attack:
                    how = "attack"
                elif how is None:
                    continue  # nothing to dial: inside addresses are not routed from outside
                else:
                    addr = FLOATING_IP if dst.name == "vip" else BASTION_FIP
            for proto, ports in (("tcp", TCP_PORTS), ("udp", UDP_PORTS)):
                for port in ports:
                    out.append(Probe(
                        src.name, dst.name, net, addr, proto, port,
                        expected_open(spec, src, dst, net, proto, port),
                        admits(spec, src.group, dst.group, net, proto, port), how or "none"))
    return out

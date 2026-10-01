"""Turns security-groups.yaml and platform.yaml into per-host firewall rules.

One implementation, two users: the `hardening` role calls these as Jinja filters when it renders
nftables.conf, and tests/isolation imports the same functions to build its lab, so the lab and
the real hosts cannot be given different rules.
"""

from __future__ import annotations

import ipaddress
from typing import Any

INTERNET = "internet"
ANYWHERE = "0.0.0.0/0"


def group_of(spec: dict[str, Any], host: str) -> str | None:
    """The security group a named host belongs to (each host is in at most one)."""
    found = [g for g, hosts in spec["members"].items() if host in hosts]
    if len(found) > 1:
        raise ValueError(f"{host} is in more than one group: {found}")
    return found[0] if found else None


def host_addresses(platform: dict[str, Any], host: str) -> list[str]:
    """Every address a host has, on every network."""
    return sorted(set(platform["network"]["hosts"][host].values()))


def group_addresses(spec: dict[str, Any], platform: dict[str, Any], group: str,
                    net: str | None = None) -> list[str]:
    """Addresses and ranges that count as being in `group`, optionally only on one network.
    The sandbox pool is a range; the gateways' edge side includes the VIP they share."""
    hosts = platform["network"]["hosts"]
    out: list[str] = []
    for h in spec["members"].get(group, []):
        out += [a for n, a in hosts[h].items() if net in (None, n)]
    if group == "sg-gateway" and net in (None, "edge"):
        out.append(platform["network"]["vip"])
    if group == "sg-sandbox" and net in (None, "sandbox"):
        pool = platform["ip_pool"]
        out.append(f"{pool['first']}-{pool['last']}")
    return out


def ingress(spec: dict[str, Any], platform: dict[str, Any], group: str) -> list[dict[str, Any]]:
    """Ingress rules of `group` with remote groups resolved to addresses. `dests` are this
    group's own addresses on the rule's network, so the rule is as narrow as the cloud's."""
    rules = []
    for r in spec["groups"][group]["ingress"]:
        sources = [ANYWHERE] if r["from"] == INTERNET else group_addresses(spec, platform, r["from"])
        rules.append({"sources": sources, "dests": group_addresses(spec, platform, group, r["network"]),
                      "network": r["network"], "proto": r["proto"], "ports": r.get("ports", []),
                      "from": r["from"], "why": r["why"]})
    return rules


def host_ingress(spec: dict[str, Any], platform: dict[str, Any], host: str) -> list[dict[str, Any]]:
    group = group_of(spec, host)
    return ingress(spec, platform, group) if group else []


def problems(spec: dict[str, Any], platform: dict[str, Any]) -> list[str]:
    """Cross-reference mistakes JSON Schema cannot see."""
    out = []
    hosts = set(platform["network"]["hosts"])
    for group, members in spec["members"].items():
        if group not in spec["groups"]:
            out.append(f"members names {group}, which has no rules")
        for h in members:
            if h not in hosts:
                out.append(f"{group} lists {h}, which platform.yaml does not define")
    for h in sorted(hosts):
        try:
            if group_of(spec, h) is None:
                out.append(f"{h} is in no security group")
        except ValueError as e:
            out.append(str(e))
    for group, body in spec["groups"].items():
        for r in body["ingress"]:
            if r["from"] != INTERNET and r["from"] not in spec["groups"]:
                out.append(f"{group} takes traffic from unknown group {r['from']}")
        for r in body["ingress"]:
            if not group_addresses(spec, platform, group, r["network"]):
                out.append(f"{group} has no port on {r['network']}, so a rule there can never apply")
        if group == "sg-sandbox" and any(r["from"] == INTERNET for r in body["ingress"]):
            out.append("sg-sandbox must never accept traffic from the internet")
        if group != "sg-gateway" and any(
                r["from"] == INTERNET and r["proto"] != "udp" for r in body["ingress"]):
            out.append(f"{group} takes TCP from the internet; only sg-gateway may")
    for g in spec["groups"]:
        for r in spec["groups"][g]["ingress"]:
            if r["from"] == INTERNET and g == "sg-gateway":
                bad = set(r.get("ports", [])) - set(spec["public"]["floating_ip_ports"])
                if bad:
                    out.append(f"sg-gateway opens {sorted(bad)} to the internet")
    return out


def nth_host(cidr: str, n: int) -> str:
    """The n-th address of a network, counting the network address as 0."""
    return str(ipaddress.ip_network(cidr)[n])


def iface_for(facts: dict[str, Any], address: str) -> str:
    """The interface holding `address`, from gathered facts, so roles do not guess eth0 or ens3
    (the order Nova attaches ports in is not the order they are listed in)."""
    for name in facts.get("interfaces", []):
        info = facts.get(name.replace("-", "_"), {})
        v4 = [info.get("ipv4", {}), *info.get("ipv4_secondaries", [])]
        if any(a.get("address") == address for a in v4):
            return str(name)
    raise ValueError(f"no interface has {address}")


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "ipo_host_ingress": host_ingress,
            "ipo_group_of": group_of,
            "ipo_group_addresses": group_addresses,
            "ipo_nth_host": nth_host,
            "ipo_iface_for": iface_for,
        }

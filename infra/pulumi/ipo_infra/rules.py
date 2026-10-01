"""security-groups.yaml as Neutron security groups, without touching Pulumi (so it is tested
directly, and policy/ can check the same data).

Neutron applies groups per port, while the spec scopes each rule to one network. So there is one
Neutron group per spec group and network, named `<group>.<network>` (`sg-gateway.edge`), each
port gets the one for its network, and a rule from a spec group references every network group
of that spec group, which is "any address of a member", as the isolation lab models it.
"""

from dataclasses import dataclass
from typing import Any

from ipo_infra.spec import ipo_net

ANYWHERE = "0.0.0.0/0"
PROTOCOLS = {"tcp": "tcp", "udp": "udp", "vrrp": "vrrp", "any": None}


@dataclass(frozen=True)
class Rule:
    group: str  # the Neutron group the rule belongs to, e.g. sg-control.mgmt
    protocol: str | None  # None: every protocol
    port_min: int | None
    port_max: int | None
    remote_group: str | None  # a Neutron group name, or None with remote_ip_prefix
    remote_ip_prefix: str | None
    description: str

    @property
    def name(self) -> str:
        """The Pulumi resource name: stable, readable, and starting with the group's name."""
        ports = "" if self.port_min is None else f"-{self.port_min}" + (
            f"-{self.port_max}" if self.port_max != self.port_min else "")
        source = self.remote_group or "internet"
        return f"{self.group}-from-{source}-{self.protocol or 'any'}{ports}"


def networks_of(groups: dict[str, Any], platform: dict[str, Any], group: str) -> list[str]:
    """The networks a spec group has ports on, in a fixed order."""
    nets: set[str] = set()
    for host in groups["members"].get(group, []):
        nets |= set(platform["network"]["hosts"][host])
    if group == "sg-sandbox":
        nets.add("sandbox")
    if group in groups.get("ephemeral", []):
        nets |= {r["network"] for r in groups["groups"][group]["ingress"]}
    return [n for n in ("edge", "sandbox", "mgmt") if n in nets]


def neutron_groups(groups: dict[str, Any], platform: dict[str, Any]) -> list[str]:
    return [f"{g}.{n}" for g in groups["groups"] for n in networks_of(groups, platform, g)]


def port_ranges(ports: list[int]) -> list[tuple[int, int]]:
    """Contiguous runs, so [4317, 4318] is one Neutron rule and [80, 443] two."""
    out: list[tuple[int, int]] = []
    for p in sorted(ports):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def rules(groups: dict[str, Any], platform: dict[str, Any]) -> list[Rule]:
    out = []
    for group, body in groups["groups"].items():
        for r in body["ingress"]:
            target = f"{group}.{r['network']}"
            if r["from"] == ipo_net.INTERNET:
                remotes: list[tuple[str | None, str | None]] = [(None, ANYWHERE)]
            else:
                remotes = [(f"{r['from']}.{n}", None) for n in networks_of(groups, platform, r["from"])]
            ranges: list[tuple[int | None, int | None]] = (
                list(port_ranges(r["ports"])) if "ports" in r else [(None, None)])
            for remote_group, prefix in remotes:
                for lo, hi in ranges:
                    out.append(Rule(target, PROTOCOLS[r["proto"]], lo, hi, remote_group, prefix,
                                    f"{r['from']}: {r['why']}"))
    return out

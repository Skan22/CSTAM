"""The guardrails as plain functions of a resource's type, Pulumi name and inputs (engine
property names), so the tests call them directly; __main__.py wraps them for CrossGuard.
Each returns a violation message, or None."""

from typing import Any

WORLD = {"0.0.0.0/0", "::/0"}
# What may be open to the whole internet, by security group: the plan's 80 and 443 on the
# gateways, plus WireGuard on the bastion, which is how operators get in at all.
PUBLIC = {"sg-gateway.": {("tcp", 80), ("tcp", 443)}, "sg-bastion.": {("udp", 51820)}}
ROLES = {"gateways", "control_plane", "relays", "bastions"}

RULE = "openstack:networking/secGroupRule:SecGroupRule"
PORT = "openstack:networking/port:Port"
NETWORK = "openstack:networking/network:Network"
SUBNET = "openstack:networking/subnet:Subnet"
INSTANCE = "openstack:compute/instance:Instance"


def world_ingress(rtype: str, name: str, props: dict[str, Any]) -> str | None:
    if rtype != RULE or props.get("direction") != "ingress" or props.get("remoteIpPrefix") not in WORLD:
        return None
    lo, hi, proto = props.get("portRangeMin"), props.get("portRangeMax"), props.get("protocol")
    for group, allowed in PUBLIC.items():
        if group in name and lo == hi and (proto, lo) in allowed:
            return None
    return (f"{name} opens {proto or 'every protocol'} {lo}-{hi} to the internet; only "
            "80 and 443 on sg-gateway and WireGuard on sg-bastion may be")


def port_security(rtype: str, name: str, props: dict[str, Any]) -> str | None:
    if rtype in (PORT, NETWORK) and props.get("portSecurityEnabled") is False:
        return f"{name} turns port security off; without it a sandbox can send as another"
    return None


def instance_role(rtype: str, name: str, props: dict[str, Any]) -> str | None:
    role = (props.get("metadata") or {}).get("role")
    if rtype == INSTANCE and role not in ROLES:
        return f"{name} has role={role!r}; the Ansible inventory groups VMs by one of {sorted(ROLES)}"
    return None


def sandbox_dhcp(rtype: str, name: str, props: dict[str, Any]) -> str | None:
    if rtype == SUBNET and "sandbox" in name and props.get("enableDhcp") is not False:
        return f"{name} runs DHCP; on sandbox-net the control plane assigns every address"
    return None


ALL = {
    "no-world-ingress": (world_ingress, "Nothing but 80/443 on the gateways and WireGuard on the bastion "
                                        "is open to 0.0.0.0/0"),
    "port-security-on": (port_security, "Port security stays on for every port and network"),
    "vm-has-role": (instance_role, "Every VM carries the role tag the Ansible inventory groups by"),
    "sandbox-no-dhcp": (sandbox_dhcp, "sandbox-net has no DHCP"),
}

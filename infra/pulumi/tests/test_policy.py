"""The guardrails catch what they are for and pass what the platform needs."""

import pytest

from policy import checks

RULE = checks.RULE


@pytest.mark.parametrize("name,proto,port,ok", [
    ("sg-gateway.edge-from-internet-tcp-443", "tcp", 443, True),
    ("demo-sg-gateway.edge-from-internet-tcp-80", "tcp", 80, True),
    ("sg-bastion.mgmt-from-internet-udp-51820", "udp", 51820, True),
    ("sg-gateway.edge-from-internet-tcp-22", "tcp", 22, False),
    ("sg-control.mgmt-from-internet-tcp-443", "tcp", 443, False),
    ("sg-bastion.mgmt-from-internet-tcp-22", "tcp", 22, False),
    ("sg-gateway.edge-from-internet-any", None, None, False),
])
def test_world_ingress(name: str, proto: str | None, port: int | None, ok: bool) -> None:
    props = {"direction": "ingress", "protocol": proto, "portRangeMin": port, "portRangeMax": port,
             "remoteIpPrefix": "0.0.0.0/0"}
    assert (checks.world_ingress(RULE, name, props) is None) == ok


def test_a_range_including_80_is_not_80() -> None:
    props = {"direction": "ingress", "protocol": "tcp", "portRangeMin": 1, "portRangeMax": 1024,
             "remoteIpPrefix": "0.0.0.0/0"}
    assert checks.world_ingress(RULE, "sg-gateway.edge-x", props)


def test_rules_from_a_group_or_egress_are_not_world_ingress() -> None:
    assert checks.world_ingress(RULE, "x", {"direction": "ingress", "remoteGroupId": "g"}) is None
    assert checks.world_ingress(RULE, "x", {"direction": "egress", "remoteIpPrefix": "0.0.0.0/0"}) is None


def test_port_security_off_is_refused_on_ports_and_networks() -> None:
    assert checks.port_security(checks.PORT, "p", {"portSecurityEnabled": False})
    assert checks.port_security(checks.NETWORK, "n", {"portSecurityEnabled": False})
    assert checks.port_security(checks.PORT, "p", {"portSecurityEnabled": True}) is None


def test_every_vm_needs_a_known_role() -> None:
    assert checks.instance_role(checks.INSTANCE, "vm", {"metadata": {}})
    assert checks.instance_role(checks.INSTANCE, "vm", {"metadata": {"role": "gateway"}})
    assert checks.instance_role(checks.INSTANCE, "vm", {"metadata": {"role": "gateways"}}) is None


def test_sandbox_net_must_not_run_dhcp() -> None:
    assert checks.sandbox_dhcp(checks.SUBNET, "sandbox-subnet", {"enableDhcp": True})
    assert checks.sandbox_dhcp(checks.SUBNET, "sandbox-subnet", {})
    assert checks.sandbox_dhcp(checks.SUBNET, "sandbox-subnet", {"enableDhcp": False}) is None
    assert checks.sandbox_dhcp(checks.SUBNET, "edge-subnet", {"enableDhcp": True}) is None

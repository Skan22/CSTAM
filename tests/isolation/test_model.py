"""The oracle and the specs, with no network involved."""

import json
from pathlib import Path

import jsonschema
import pytest

from isolation import model
from isolation.model import ipo_net

platform, spec = model.load()
NODES = model.topology(platform, spec)


def test_security_groups_validate_and_cross_reference() -> None:
    schema = json.loads((model.REPO / "security-groups.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(spec)
    assert ipo_net.problems(spec, platform) == []


def test_the_schema_rejects_a_tcp_rule_without_ports_and_a_vrrp_rule_with_them() -> None:
    schema = json.loads((model.REPO / "security-groups.schema.json").read_text())
    v = jsonschema.Draft202012Validator(schema)
    base = {"members": {}, "groups": {}, "public": {"floating_ip_ports": [80]}}
    rule = {"network": "mgmt", "from": "sg-bastion", "why": "because"}
    for bad in ({**rule, "proto": "tcp"}, {**rule, "proto": "vrrp", "ports": [1]}):
        assert list(v.iter_errors({**base, "groups": {"sg-x": {"ingress": [bad]}}}))


@pytest.mark.parametrize("mutate,expected", [
    (lambda s: s["groups"]["sg-sandbox"]["ingress"].append(
        {"network": "sandbox", "from": "internet", "proto": "tcp", "ports": [22], "why": "oops"}),
     "sg-sandbox must never accept traffic from the internet"),
    (lambda s: s["groups"]["sg-control"]["ingress"].append(
        {"network": "mgmt", "from": "internet", "proto": "tcp", "ports": [5432], "why": "oops"}),
     "sg-control takes TCP from the internet; only sg-gateway may"),
    (lambda s: s["groups"]["sg-gateway"]["ingress"][0]["ports"].append(22),
     "sg-gateway opens [22] to the internet"),
    (lambda s: s["groups"]["sg-relay"]["ingress"].append(
        {"network": "mgmt", "from": "sg-nope", "proto": "any", "why": "typo"}),
     "sg-relay takes traffic from unknown group sg-nope"),
    (lambda s: s["members"]["sg-control"].remove("cp-2"), "cp-2 is in no security group"),
])
def test_the_cross_checks_catch_the_mistakes_that_open_the_network(mutate, expected) -> None:  # type: ignore[no-untyped-def]
    import copy
    bad = copy.deepcopy(spec)
    mutate(bad)
    assert expected in ipo_net.problems(bad, platform)


def open_set(src: str) -> set[tuple[str, str, str, int]]:
    return {(p.dst, p.net, p.proto, p.port) for p in model.probes_from(platform, spec, src)
            if p.expect_open}


def test_a_sandbox_can_reach_only_the_relay_telemetry_ports_and_the_public_gateway_ports() -> None:
    assert open_set("sbx-a") == {
        ("relay", "sandbox", "tcp", 4317), ("relay", "sandbox", "tcp", 4318),
        ("relay", "sandbox", "udp", 5140),
        # Through r-edge, like anyone: the gateways' public ports, nothing else.
        *{(g, "edge", "tcp", p) for g in ("gw-a", "gw-b", "vip") for p in (80, 443)},
    }


def test_no_sandbox_path_to_another_sandbox_or_to_the_management_plane() -> None:
    for p in model.probes_from(platform, spec, "sbx-a"):
        if p.dst in ("sbx-b", "cp-1", "cp-2", "bastion") or p.net == "mgmt":
            assert not p.expect_open, p.name


def test_the_internet_reaches_the_floating_ip_on_web_ports_and_the_bastion_on_wireguard() -> None:
    assert open_set("internet") == {
        ("vip", "edge", "tcp", 80), ("vip", "edge", "tcp", 443), ("bastion", "mgmt", "udp", 51820)}


def test_the_agent_port_is_open_to_the_control_plane_only() -> None:
    for src in ("cp-1", "cp-2"):
        assert ("gw-a", "mgmt", "tcp", 8443) in open_set(src)
    for src in ("sbx-a", "relay", "gw-b", "internet"):
        assert not any(port == 8443 for _, net, _, port in open_set(src) if net == "mgmt")


def test_every_rule_in_the_spec_is_exercised_by_some_probe() -> None:
    """A rule nobody probes could be wrong without anyone noticing."""
    hit = set()
    for name in NODES:
        for p in model.probes_from(platform, spec, name):
            if p.expect_open:
                hit.add((NODES[p.dst].group, p.net, p.proto, p.port))
    for group, body in spec["groups"].items():
        for r in body["ingress"]:
            if r["proto"] == "vrrp":
                continue  # not a port: the lab checks it with the keepalived pair
            ports = r.get("ports") or [model.TCP_PORTS[0]]
            for port in ports:
                assert (group, r["network"], r["proto"] if r["proto"] != "any" else "tcp", port) in hit, (
                    f"nothing probes {group} {r}")


def test_probe_addresses_are_the_dialled_ones() -> None:
    p = next(p for p in model.probes_from(platform, spec, "internet") if p.dst == "vip")
    assert p.addr == model.FLOATING_IP and p.how == "fip"
    p = next(p for p in model.probes_from(platform, spec, "sbx-a") if p.dst == "gw-a" and p.net == "edge")
    assert p.addr == "10.0.0.11" and p.how == "routed"
    attack = model.probes_from(platform, spec, "internet", attack=True)
    assert {p.addr for p in attack if p.dst == "cp-1"} == {"10.30.0.21"}


def test_a_rerouting_internet_may_get_only_what_the_policy_gives_anyone() -> None:
    allowed = {(p.dst, p.net, p.proto, p.port)
               for p in model.probes_from(platform, spec, "internet", attack=True) if p.policy_open}
    assert allowed == {
        *{(g, "edge", "tcp", port) for g in ("gw-a", "gw-b", "vip") for port in (80, 443)},
        ("bastion", "mgmt", "udp", 51820)}


def test_the_only_weak_host_exposure_is_the_relays_mgmt_address_on_its_sandbox_ports() -> None:
    """What Neutron's security groups alone would let a sandbox reach by routing another address
    via a neighbour's sandbox port. The host firewall pins destinations, which closes it."""
    sbx = NODES["sbx-a"]
    found = {(p.dst, p.net, p.proto, p.port) for p in model.probes_from(platform, spec, "sbx-a")
             if not p.policy_open
             and model.weak_host_open(spec, sbx, NODES[p.dst], p.net, p.proto, p.port)}
    assert found == {("relay", "mgmt", "tcp", 4317), ("relay", "mgmt", "tcp", 4318),
                     ("relay", "mgmt", "udp", 5140)}


def test_the_spec_files_are_where_the_pulumi_and_ansible_code_will_look() -> None:
    assert Path(model.REPO / "security-groups.yaml").is_file()
    assert (model.REPO / "ansible/filter_plugins/ipo_net.py").is_file()


def test_the_interface_filter_finds_an_address_among_the_facts() -> None:
    facts = {"interfaces": ["lo", "ens3", "ens4"], "lo": {"ipv4": {"address": "127.0.0.1"}},
             "ens3": {"ipv4": {"address": "10.30.0.11"}},
             "ens4": {"ipv4": {"address": "10.0.0.11"},
                      "ipv4_secondaries": [{"address": "10.0.0.100"}]}}
    assert ipo_net.iface_for(facts, "10.0.0.11") == "ens4"
    assert ipo_net.iface_for(facts, "10.0.0.100") == "ens4"
    with pytest.raises(ValueError):
        ipo_net.iface_for(facts, "10.20.0.2")
    assert ipo_net.nth_host("10.0.0.0/24", 1) == "10.0.0.1"

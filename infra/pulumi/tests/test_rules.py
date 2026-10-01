"""security-groups.yaml as Neutron groups and rules, checked against the spec it came from."""

from ipo_infra import rules, spec
from policy import checks

platform, groups = spec.load()
RULES = rules.rules(groups, platform)


def test_one_neutron_group_per_spec_group_and_network() -> None:
    assert set(rules.neutron_groups(groups, platform)) == {
        "sg-gateway.edge", "sg-gateway.sandbox", "sg-gateway.mgmt", "sg-sandbox.sandbox",
        "sg-control.mgmt", "sg-relay.sandbox", "sg-relay.mgmt", "sg-bastion.mgmt", "sg-builder.mgmt"}


def test_contiguous_ports_become_one_rule() -> None:
    assert rules.port_ranges([443, 80]) == [(80, 80), (443, 443)]
    assert rules.port_ranges([4318, 4317, 9090]) == [(4317, 4318), (9090, 9090)]


def test_the_rules_say_exactly_what_the_spec_says() -> None:
    """Rebuilt from the Neutron rules, the spec comes back: nothing added, nothing lost."""
    rebuilt: dict[tuple[str, str, str, str], set[int]] = {}
    sources: dict[tuple[str, str, str, str], set[str]] = {}
    for r in RULES:
        group, net = r.group.split(".")
        src = "internet" if r.remote_ip_prefix else (r.remote_group or "").split(".")[0]
        proto = {"tcp": "tcp", "udp": "udp", "vrrp": "vrrp", None: "any"}[r.protocol]
        key = (group, net, src, proto)
        if r.port_min is not None and r.port_max is not None:
            rebuilt.setdefault(key, set()).update(range(r.port_min, r.port_max + 1))
        else:
            rebuilt.setdefault(key, set())
        sources.setdefault(key, set()).add(r.remote_group or "internet")
    expected: dict[tuple[str, str, str, str], set[int]] = {}
    for group, body in groups["groups"].items():
        for r in body["ingress"]:
            expected.setdefault((group, r["network"], r["from"], r["proto"]), set()).update(
                r.get("ports", []))
    assert rebuilt == expected
    # A rule from a group references every network group of it: any address of a member.
    assert sources[("sg-control", "mgmt", "sg-gateway", "tcp")] == {
        "sg-gateway.edge", "sg-gateway.sandbox", "sg-gateway.mgmt"}


def test_only_the_gateways_web_ports_and_wireguard_face_the_internet() -> None:
    world = {(r.group, r.protocol, r.port_min) for r in RULES if r.remote_ip_prefix}
    assert world == {("sg-gateway.edge", "tcp", 80), ("sg-gateway.edge", "tcp", 443),
                     ("sg-bastion.mgmt", "udp", 51820)}


def test_every_rule_passes_the_policy_pack() -> None:
    for r in RULES:
        props = {"direction": "ingress", "protocol": r.protocol, "portRangeMin": r.port_min,
                 "portRangeMax": r.port_max, "remoteIpPrefix": r.remote_ip_prefix}
        assert checks.world_ingress(checks.RULE, r.name, props) is None, r


def test_rule_names_are_unique() -> None:
    assert len({r.name for r in RULES}) == len(RULES)

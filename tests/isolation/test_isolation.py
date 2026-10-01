"""The isolation suite (plan M7), in the namespace lab: `uv run python -m isolation.run`.

Every check probes real sockets through both enforcement layers (see lab.py) and compares what
got through with the policy in security-groups.yaml. The second half turns layers off one at a
time, which proves the probes can see an open path, that each layer covers what it should, and
what each layer alone would let through.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest

from isolation import model
from isolation.lab import IsolationLab, Result

SANDBOXES = ("sbx-a", "sbx-b")
GATEWAYS = ("gw-a", "gw-b", "vip")


def leaks(results: list[Result]) -> list[Result]:
    return [r for r in results if r.reachable and not r.probe.expect_open]


def broken(results: list[Result]) -> list[Result]:
    return [r for r in results if r.probe.expect_open and r.verdict != "open"]


def show(results: list[Result]) -> str:
    lines = [str(r) for r in results[:40]]
    return "\n".join(lines + ([f"... and {len(results) - 40} more"] if len(results) > 40 else []))


def assert_matches_policy(results: list[Result]) -> None:
    assert results
    assert not leaks(results), f"open but the policy says closed:\n{show(leaks(results))}"
    assert not broken(results), f"closed but the policy says open:\n{show(broken(results))}"


def sweep(lab: IsolationLab, src: str, dst: str, net: str, ports: range) -> list[Result]:
    """Every TCP port of one address: nothing listens on most of them, so a path that is open
    answers with a reset, and only a filtered one stays silent."""
    addr = lab.nodes[dst].addrs[net]
    return lab.probe({src: [model.Probe(src, dst, net, addr, "tcp", port, False, False, "sweep")
                            for port in ports]})


def rerouted_sandbox(lab: IsolationLab, src: str = "sbx-a") -> list[tuple[str, Result]]:
    """A sandbox that routes edge-net and mgmt-net through each machine on its network in turn."""
    out = []
    for owner, via in lab.neighbours(src):
        lab.reroute(src, ["edge", "mgmt"], via)
        try:
            out += [(owner, r) for r in lab.matrix([src])]
        finally:
            lab.restore_routes(src)
    return out


def rerouted_internet(lab: IsolationLab) -> list[Result]:
    """The internet routing every inside network through each router's external address."""
    out = []
    for router in ("r-edge", "r-mgmt"):
        lab.reroute("internet", ["edge", "sandbox", "mgmt"], lab.router_addr(router, "ext"))
        try:
            out += lab.matrix(["internet"], attack=True)
        finally:
            lab.restore_routes("internet")
    return out


def forge(lab: IsolationLab) -> set[str]:
    """sbx-a sends syslog datagrams to the relay (which takes UDP 5140 from any sandbox) under
    various identities; returns the ones the relay received."""
    a, b = lab.port("sbx-a", "sandbox"), lab.port("sbx-b", "sandbox")
    first = lab.platform["ip_pool"]["first"].rsplit(".", 1)
    unused = f"{first[0]}.{int(first[1]) + 50}"
    identities = {
        "honest": (a.mac, a.ip),
        "another sandbox's IP": (a.mac, b.ip),
        "another sandbox's MAC": (b.mac, a.ip),
        "another sandbox's MAC and IP": (b.mac, b.ip),
        "an unused pool address": (a.mac, unused),
        "a gateway's address": (a.mac, lab.port("gw-a", "sandbox").ip),
    }
    tag = uuid.uuid4().hex[:10]
    relay = lab.port("relay", "sandbox")
    for name, (mac, ip) in identities.items():
        for _ in range(2):
            lab.send_frame("sbx-a", "sandbox", relay, src_mac=mac, src_ip=ip, dport=5140,
                           payload=f"{tag}:{name}")
    got = {r.payload.split(":", 1)[1] for r in lab.delivered(tag)}
    lab.forget()  # a forged MAC may have taught the bridge sbx-b's address on sbx-a's port
    return got


# --------------------------------------------------------------------- the plan's checks


def test_sandbox_a_cannot_reach_sandbox_b_on_any_port(secured: IsolationLab) -> None:
    results = [r for r in secured.matrix(list(SANDBOXES)) if r.probe.dst in SANDBOXES]
    assert results and not any(r.reachable for r in results), show(leaks(results))
    swept = sweep(secured, "sbx-a", "sbx-b", "sandbox", range(1, 2049))
    assert not any(r.reachable for r in swept), show([r for r in swept if r.reachable])


def test_sandbox_cannot_reach_a_gateways_management_address_or_the_agent_port(
        secured: IsolationLab) -> None:
    results = [r for r in secured.matrix(list(SANDBOXES)) if r.probe.dst in GATEWAYS]
    mgmt_or_agent = [r for r in results if r.probe.net == "mgmt" or r.probe.port == 8443]
    assert mgmt_or_agent and not any(r.reachable for r in mgmt_or_agent), show(leaks(mgmt_or_agent))
    # What does get through is the public web ports, the same as from the internet.
    assert {(r.probe.net, r.probe.port) for r in results if r.reachable} == {
        ("edge", 80), ("edge", 443)}


def test_sandbox_cannot_reach_anything_on_mgmt_net(secured: IsolationLab) -> None:
    results = [r for r in secured.matrix(list(SANDBOXES)) if r.probe.net == "mgmt"]
    dialled = {(r.probe.dst, r.probe.port) for r in results}
    for dst, port in (("cp-1", 8000), ("cp-1", 5432), ("cp-1", 3000), ("cp-2", 5432),
                      ("relay", 9090), ("bastion", 22)):
        assert (dst, port) in dialled  # control plane, Postgres, Grafana, Prometheus, SSH
    assert not any(r.reachable for r in results), show(leaks(results))


def test_sandbox_cannot_send_with_another_sandboxs_ip_or_mac(secured: IsolationLab) -> None:
    assert forge(secured) == {"honest"}


def test_internet_reaches_only_80_and_443_on_the_floating_ip(secured: IsolationLab) -> None:
    results = secured.matrix(["internet"])
    fip = [r for r in results if r.probe.addr == model.FLOATING_IP]
    assert {(r.probe.proto, r.probe.port) for r in fip if r.reachable} == {("tcp", 80), ("tcp", 443)}
    bastion = [r for r in results if r.probe.addr == model.BASTION_FIP]
    assert {(r.probe.proto, r.probe.port) for r in bastion if r.reachable} == {("udp", 51820)}
    assert_matches_policy(results)


# --------------------------------------------------------------------- beyond the plan


def test_every_machine_reaches_exactly_what_the_policy_allows(secured: IsolationLab) -> None:
    """All sources, all destinations, all their addresses, all probe ports. The open ones are the
    positive controls: a probe that can never connect proves nothing when it is blocked."""
    results = secured.matrix()
    assert_matches_policy(results)
    assert sum(r.reachable for r in results) > 50


def test_a_sandbox_routing_through_its_neighbours_gets_nothing_more(secured: IsolationLab) -> None:
    found = [(owner, r) for owner, r in rerouted_sandbox(secured)
             if r.reachable and not r.probe.policy_open]
    assert not found, "\n".join(f"via {o}: {r}" for o, r in found)


def test_the_internet_routing_into_the_platform_gets_nothing_more(secured: IsolationLab) -> None:
    results = rerouted_internet(secured)
    found = [r for r in results if r.reachable and not r.probe.policy_open]
    assert not found, show(found)
    # The routers do forward, so the policy's own openings are reachable on inside addresses too.
    assert any(r.reachable for r in results if r.probe.dst == "gw-a" and r.probe.port == 443)


def test_only_the_gateways_can_send_vrrp_to_each_other(secured: IsolationLab) -> None:
    """A forged advert with a higher priority would take the VIP: a split brain on demand."""
    lab = secured
    tag = uuid.uuid4().hex[:10]
    gw_b = lab.nodes["gw-b"].addrs
    sends = [("gw-a", gw_b["edge"], "peer"), ("sbx-a", gw_b["edge"], "sandbox"),
             ("sbx-a", lab.platform["network"]["vip"], "sandbox-to-vip"),
             ("cp-1", gw_b["mgmt"], "control"), ("relay", gw_b["sandbox"], "relay")]
    for src, dst, name in sends:
        lab.send_ipproto(src, 112, dst, f"{tag}:{name}")
    lab.reroute("internet", ["edge"], lab.router_addr("r-edge", "ext"))
    try:
        lab.send_ipproto("internet", 112, gw_b["edge"], f"{tag}:internet")
    finally:
        lab.restore_routes("internet")
    got = {r.payload.split(":", 1)[1] for r in lab.delivered(tag)}
    assert got == {"peer"}


def test_one_sandbox_flooding_the_relay_does_not_drown_the_others(secured: IsolationLab) -> None:
    """The relay's per-source limit (inventory/group_vars/relay.yml: 100/s, burst 200) cuts a
    two-second flood of 1000 datagrams a second to about 200 + 2 x 100, while another sandbox
    still gets every datagram through. Without the host layer the whole flood arrives, so the
    limit is what cut it."""
    lab = secured
    relay = lab.nodes["relay"].addrs["sandbox"]
    tag = uuid.uuid4().hex[:10]
    lab.flood("sbx-a", relay, 5140, 2000, 1000, f"{tag}:a")
    lab.flood("sbx-b", relay, 5140, 20, 1000, f"{tag}:b")
    got = lab.delivered(tag)
    from_a = sum(r.src == lab.nodes["sbx-a"].addrs["sandbox"] for r in got)
    from_b = sum(r.src == lab.nodes["sbx-b"].addrs["sandbox"] for r in got)
    assert 250 <= from_a <= 650, from_a
    assert from_b == 20

    lab.set_layers(host=False, fabric=True)
    tag = uuid.uuid4().hex[:10]
    lab.flood("sbx-a", relay, 5140, 2000, 1000, f"{tag}:a")
    assert len(lab.delivered(tag)) > 1800


def test_without_the_edge_routing_unit_sandboxes_lose_the_gateways_web_ports(
        secured: IsolationLab) -> None:
    """Why the `gateway_network` role exists: without its rule the answer to a sandbox leaves by
    the gateway's sandbox port with an edge source, and port security drops it."""
    lab = secured
    web = [p for p in model.probes_from(lab.platform, lab.spec, "sbx-a")
           if p.dst in GATEWAYS and p.expect_open]
    assert len(web) == 6
    for gw in ("gw-a", "gw-b"):
        lab.edge_routing(gw, on=False)
    try:
        assert not any(r.reachable for r in lab.probe({"sbx-a": web}))
    finally:
        for gw in ("gw-a", "gw-b"):
            lab.edge_routing(gw, on=True)
    assert all(r.verdict == "open" for r in lab.probe({"sbx-a": web}))


# --------------------------------------------------------------------- one layer at a time


def test_with_no_filtering_every_path_is_open(lab: IsolationLab) -> None:
    """The control for every blocked verdict above: with both layers off, each probe that has a
    route gets an answer, so a block can only come from the filters."""
    lab.set_layers(host=False, fabric=False)
    results = lab.matrix()
    wrong = [r for r in results if r.reachable != (r.probe.how != "none")]
    assert not wrong, show(wrong)
    assert len(forge(lab)) == 6
    swept = sweep(lab, "sbx-a", "sbx-b", "sandbox", range(1, 2049))
    assert all(r.reachable for r in swept)


def test_the_host_layer_alone_guards_the_platform_but_not_one_sandbox_from_another(
        lab: IsolationLab) -> None:
    """Sandboxes run the teams' own images, so their firewalls are not the platform's to count on.
    Without the fabric every probe that lands on a sandbox gets in; nothing else does."""
    lab.set_layers(host=True, fabric=False)
    results = lab.matrix()
    assert not broken(results), show(broken(results))
    assert {r.probe for r in leaks(results)} == {
        r.probe for r in results
        if r.probe.dst in SANDBOXES and r.probe.how != "none" and not r.probe.expect_open}


def test_the_host_layer_alone_cannot_stop_spoofing_within_the_pool(lab: IsolationLab) -> None:
    """The relay takes syslog from every pool address, and a forged one looks the same to it, so
    only port security stops a sandbox writing as another. Port security must stay on."""
    lab.set_layers(host=True, fabric=False)
    assert forge(lab) == {"honest", "another sandbox's IP", "another sandbox's MAC",
                          "another sandbox's MAC and IP", "an unused pool address"}


def test_the_fabric_alone_matches_the_policy(lab: IsolationLab) -> None:
    lab.set_layers(host=False, fabric=True)
    assert_matches_policy(lab.matrix())
    assert forge(lab) == {"honest"}


def test_the_fabric_alone_lets_a_rerouted_datagram_reach_the_relays_mgmt_address(
        lab: IsolationLab) -> None:
    """Security groups filter per port and ignore the destination address, and Linux accepts any
    of its addresses on any interface. A sandbox that routes mgmt-net via the relay's sandbox
    address can therefore deliver to the relay's mgmt address whatever the relay's sandbox port
    admits. Replies leave with a source port security drops, so only one-way UDP gets through,
    and only to the listeners the sandbox may reach anyway. The host layer pins destinations,
    which closes it (see the rerouting test above)."""
    lab.set_layers(host=False, fabric=True)
    src = lab.nodes["sbx-a"]
    found = {(o, r.probe.dst, r.probe.net, r.probe.proto, r.probe.port, r.verdict)
             for o, r in rerouted_sandbox(lab) if r.reachable and not r.probe.policy_open}
    predicted = {(r.dst, r.net, r.proto, r.port) for r in model.probes_from(lab.platform, lab.spec,
                                                                           "sbx-a")
                 if r.proto == "udp" and not r.policy_open
                 and model.weak_host_open(lab.spec, src, lab.nodes[r.dst], r.net, r.proto, r.port)}
    assert predicted == {("relay", "mgmt", "udp", 5140)}
    assert found == {("relay", *p, "one-way") for p in predicted}


def open_postgres_to_the_relay(spec: dict[str, Any]) -> dict[str, Any]:
    bad = copy.deepcopy(spec)
    bad["groups"]["sg-control"]["ingress"].append(
        {"network": "mgmt", "from": "sg-relay", "proto": "tcp", "ports": [5432], "why": "a mistake"})
    return bad


@pytest.mark.parametrize("layer", ["host", "fabric"])
def test_a_wrong_rule_in_one_layer_is_caught_and_the_other_layer_still_holds(
        lab: IsolationLab, layer: str) -> None:
    bad = open_postgres_to_the_relay(lab.spec)
    injected = {("relay", "cp-1", 5432), ("relay", "cp-2", 5432)}

    lab.set_layers(host=True, fabric=True, **{f"{layer}_spec": bad})
    assert_matches_policy(lab.matrix())

    lab.set_layers(host=layer == "host", fabric=layer == "fabric", **{f"{layer}_spec": bad})
    found = {(r.probe.src, r.probe.dst, r.probe.port) for r in leaks(lab.matrix())
             if r.probe.dst not in SANDBOXES}
    assert found == injected

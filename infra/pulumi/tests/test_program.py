"""The whole program under Pulumi's mocks: every resource it would create, checked for the plan's
settings (M1) and run through the policy pack. No cloud is contacted."""

from typing import Any

import pulumi
import pytest

from ipo_infra import rules, spec
from policy import checks


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[tuple[str, str, dict[str, Any]]] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str, dict[str, Any]]:
        self.resources.append((args.typ, args.name, dict(args.inputs)))
        outputs = dict(args.inputs)
        if args.typ == "openstack:networking/floatingIp:FloatingIp":
            outputs["address"] = "198.51.100.7"
        return f"{args.name}-id", outputs

    def call(self, args: pulumi.runtime.MockCallArgs) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        if args.token == "openstack:networking/getNetwork:getNetwork":
            return {"id": "ext-net-id", "name": args.args.get("name")}, []
        return {}, []


MOCKS = Mocks()
pulumi.runtime.set_mocks(MOCKS, preview=False)

from ipo_infra import program  # noqa: E402  (after the mocks, as Pulumi requires)

PLATFORM, GROUPS = spec.load()
BUILT = program.build(PLATFORM, GROUPS, key_pair="ipo-ops", public_key="ssh-ed25519 AAAA test")


def of_type(kind: str) -> list[tuple[str, dict[str, Any]]]:
    return [(name, inputs) for typ, name, inputs in MOCKS.resources if typ.endswith(kind)]


@pytest.fixture(scope="module")
def created() -> list[tuple[str, str, dict[str, Any]]]:
    """Waits for every registration: each output depends on resources across the program."""
    outs = BUILT.outputs()

    @pulumi.runtime.test  # type: ignore[untyped-decorator]
    def wait() -> pulumi.Output[Any]:
        return pulumi.Output.all(outs["floating_ip"], outs["bastion_floating_ip"], outs["port_ids"],
                                 outs["security_groups"], outs["vip_port_id"])

    wait()
    return MOCKS.resources


def test_the_sandbox_subnet_has_no_dhcp_and_the_ipam_range_as_its_pool(created: Any) -> None:
    subnets = dict(of_type(":Subnet"))
    sandbox = subnets["sandbox-subnet"]
    assert sandbox["enableDhcp"] is False
    pool = PLATFORM["ip_pool"]
    assert sandbox["allocationPools"] == [{"start": pool["first"], "end": pool["last"]}]
    assert sandbox["cidr"] == pool["cidr"]


def test_routers_join_what_the_plan_says(created: Any) -> None:
    joins = {name for name, _ in of_type(":RouterInterface")}
    assert joins == {"r-edge-edge", "r-edge-sandbox", "r-mgmt-mgmt"}
    assert all(i["externalNetworkId"] == "ext-net-id" for _, i in of_type(":Router"))


def test_every_port_keeps_port_security_and_has_one_group_for_its_network(created: Any) -> None:
    ports = of_type(":Port")
    assert len(ports) == 1 + sum(len(a) for a in PLATFORM["network"]["hosts"].values())  # + the VIP
    for name, i in ports:
        assert i["portSecurityEnabled"] is True, name
        assert len(i["securityGroupIds"]) == 1, name
        net = name.rsplit("-", 1)[-1] if name != "vip-edge" else "edge"
        assert i["securityGroupIds"][0].split(".")[-1].startswith(net), name


def test_both_gateways_may_hold_the_vip_and_the_floating_ip_points_at_its_port(created: Any) -> None:
    ports = dict(of_type(":Port"))
    vip = PLATFORM["network"]["vip"]
    for gw in ("gw-a", "gw-b"):
        assert ports[f"{gw}-edge"]["allowedAddressPairs"] == [{"ipAddress": vip}]
    assert ports["vip-edge"]["fixedIps"][0]["ipAddress"] == vip
    assoc = dict(of_type(":FloatingIpAssociate"))
    assert assoc["fip-public-vip"]["portId"] == "vip-edge-id"
    assert assoc["fip-bastion-port"]["portId"] == "bastion-mgmt-id"


def test_vms_carry_their_role_boot_from_the_config_drive_and_keep_pairs_apart(created: Any) -> None:
    vms = dict(of_type(":Instance"))
    assert set(vms) == set(PLATFORM["network"]["hosts"])
    for i in vms.values():
        assert i["configDrive"] is True and i["imageName"] == PLATFORM["images"]["base"]
        assert i["keyPair"] == "ipo-ops"  # the key pair resource's name output
    assert {h for h, i in vms.items() if i.get("schedulerHints")} == {"gw-a", "gw-b", "cp-1", "cp-2"}
    groups = dict(of_type(":ServerGroup"))
    assert {g["policies"] for g in groups.values()} == {"soft-anti-affinity"} and len(groups) == 2
    # The edge port is a gateway's first NIC, as roles/gateway_network assumes.
    assert vms["gw-a"]["networks"][0]["port"] == "gw-a-edge-id"


def test_the_key_pair_is_created_from_the_configured_public_key(created: Any) -> None:
    [(name, i)] = of_type(":Keypair")
    assert name == "ipo-ops" and i["publicKey"] == "ssh-ed25519 AAAA test"


def test_security_groups_are_the_rules_module_output(created: Any) -> None:
    made = {name for name, _ in of_type(":SecGroupRule")}
    assert made == {r.name for r in rules.rules(GROUPS, PLATFORM)}
    assert {name for name, _ in of_type(":SecGroup")} == set(rules.neutron_groups(GROUPS, PLATFORM))


def test_the_whole_program_passes_the_policy_pack(created: Any) -> None:
    violations = [msg for typ, name, inputs in created for check, _ in checks.ALL.values()
                  if (msg := check(typ, name, inputs))]
    assert not violations
    assert len(created) > 60


def test_without_floating_ips_nothing_public_is_created() -> None:
    before = len(MOCKS.resources)
    built = program.build(PLATFORM, GROUPS, key_pair="ipo-ops", prefix="nofip-", floating_ips=False)
    assert built.gateways.fip is None and built.bastion.fip is None
    assert built.outputs()["floating_ip"] is None and built.outputs()["bastion_floating_ip"] is None
    assert not any(typ.endswith(":FloatingIp") and name.startswith("nofip-")
                   for typ, name, _ in MOCKS.resources[before:])


def test_hard_anti_affinity_can_be_asked_for() -> None:
    built = program.build(PLATFORM, GROUPS, key_pair="ipo-ops", prefix="hard-", affinity="anti-affinity")

    @pulumi.runtime.test  # type: ignore[untyped-decorator]
    def wait() -> pulumi.Output[Any]:
        return pulumi.Output.all(built.outputs()["port_ids"], built.outputs()["security_groups"])

    wait()
    groups = [i for typ, name, i in MOCKS.resources if typ.endswith(":ServerGroup")
              and name.startswith("hard-")]
    assert len(groups) == 2 and {g["policies"] for g in groups} == {"anti-affinity"}

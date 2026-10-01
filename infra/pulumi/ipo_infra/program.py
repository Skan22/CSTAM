"""The resources, as component resources so each is one reviewable unit (plan M1)."""

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi_openstack as openstack

from ipo_infra import rules as sg_rules
from ipo_infra.spec import ipo_net


class Networks(pulumi.ComponentResource):
    """edge-net, sandbox-net and mgmt-net, with r-edge (edge and sandbox) and r-mgmt."""

    def __init__(self, name: str, platform: dict[str, Any], prefix: str,
                 opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__("ipo:infra:Networks", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        n = platform["network"]
        cidrs = {"edge": n["edge_cidr"], "sandbox": platform["ip_pool"]["cidr"], "mgmt": n["mgmt_cidr"]}
        self.networks: dict[str, openstack.networking.Network] = {}
        self.subnets: dict[str, openstack.networking.Subnet] = {}
        for net, cidr in cidrs.items():
            self.networks[net] = openstack.networking.Network(
                f"{prefix}{net}-net", name=f"{prefix}{net}-net", admin_state_up=True,
                port_security_enabled=True, opts=child)
            sandbox = net == "sandbox"
            pool = platform["ip_pool"]
            self.subnets[net] = openstack.networking.Subnet(
                f"{prefix}{net}-subnet", name=f"{prefix}{net}-subnet", network_id=self.networks[net].id,
                cidr=cidr, ip_version=4, gateway_ip=str(ipaddress.ip_network(cidr)[1]),
                # sandbox-net: no DHCP, and the allocation pool is exactly the control plane's IPAM
                # range, so Neutron never hands out an address the control plane also leases.
                enable_dhcp=not sandbox,
                allocation_pools=[openstack.networking.SubnetAllocationPoolArgs(
                    start=pool["first"], end=pool["last"])] if sandbox else None,
                opts=child)
        external = openstack.networking.get_network(name=n["external_network"])
        self.external_id = external.id
        self.routers = {
            "r-edge": openstack.networking.Router(f"{prefix}r-edge", name=f"{prefix}r-edge",
                                                  external_network_id=external.id, opts=child),
            "r-mgmt": openstack.networking.Router(f"{prefix}r-mgmt", name=f"{prefix}r-mgmt",
                                                  external_network_id=external.id, opts=child),
        }
        for router, nets in (("r-edge", ("edge", "sandbox")), ("r-mgmt", ("mgmt",))):
            for net in nets:
                openstack.networking.RouterInterface(
                    f"{prefix}{router}-{net}", router_id=self.routers[router].id,
                    subnet_id=self.subnets[net].id, opts=child)
        self.register_outputs({})


class SecurityGroups(pulumi.ComponentResource):
    """One Neutron group per spec group and network, with remote-group rules (rules.py)."""

    def __init__(self, name: str, groups: dict[str, Any], platform: dict[str, Any], prefix: str,
                 opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__("ipo:infra:SecurityGroups", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        self.groups = {g: openstack.networking.SecGroup(
            f"{prefix}{g}", name=f"{prefix}{g}", description=f"security-groups.yaml: {g}",
            stateful=True, opts=child) for g in sg_rules.neutron_groups(groups, platform)}
        self.rules = [openstack.networking.SecGroupRule(
            f"{prefix}{r.name}", direction="ingress", ethertype="IPv4", protocol=r.protocol,
            port_range_min=r.port_min, port_range_max=r.port_max,
            remote_group_id=self.groups[r.remote_group].id if r.remote_group else None,
            remote_ip_prefix=r.remote_ip_prefix, security_group_id=self.groups[r.group].id,
            description=r.description, opts=child) for r in sg_rules.rules(groups, platform)]
        self.register_outputs({})


class _Hosts(pulumi.ComponentResource):
    """Shared by the four host components: a port per network with its fixed address and the
    network's security group, then the VM on those ports in platform.yaml's order."""

    role = ""  # the metadata tag the Ansible inventory groups by
    flavor = ""  # the key in platform.yaml's flavors

    def __init__(self, kind: str, name: str, hosts: Sequence[str], ctx: "Context", *,
                 anti_affinity: bool, extra_pairs: dict[str, list[str]] | None = None,
                 opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__(kind, name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        platform, nets, sgs, prefix = ctx.platform, ctx.nets, ctx.sgs, ctx.prefix
        group = ipo_net.group_of(ctx.groups, hosts[0])
        hint = None
        if anti_affinity:
            sg = openstack.compute.ServerGroup(f"{prefix}{name}-anti", name=f"{prefix}{name}-anti",
                                               policies=ctx.affinity, opts=child)
            hint = [openstack.compute.InstanceSchedulerHintArgs(group=sg.id)]
        self.ports: dict[str, dict[str, openstack.networking.Port]] = {}
        self.instances: dict[str, openstack.compute.Instance] = {}
        for host in hosts:
            self.ports[host] = {}
            for net, addr in platform["network"]["hosts"][host].items():
                self.ports[host][net] = port(
                    f"{prefix}{host}-{net}", nets, sgs, net, addr, f"{group}.{net}", child,
                    pairs=(extra_pairs or {}).get(net, []), tags=[f"role={self.role}"])
            self.instances[host] = openstack.compute.Instance(
                f"{prefix}{host}", name=f"{prefix}{host}", flavor_name=platform["flavors"][self.flavor],
                image_name=platform["images"]["base"], key_pair=ctx.key_pair, config_drive=True,
                metadata={"role": self.role}, scheduler_hints=hint,
                networks=[openstack.compute.InstanceNetworkArgs(port=p.id)
                          for p in self.ports[host].values()], opts=child)
        self.register_outputs({})


def port(name: str, nets: Networks, sgs: SecurityGroups, net: str, addr: str, sg: str,
         opts: pulumi.ResourceOptions, *, pairs: Sequence[str] = (), tags: Sequence[str] = (),
         device_owner: str | None = None) -> openstack.networking.Port:
    return openstack.networking.Port(
        name, name=name, network_id=nets.networks[net].id, admin_state_up=True,
        port_security_enabled=True, security_group_ids=[sgs.groups[sg].id],
        fixed_ips=[openstack.networking.PortFixedIpArgs(subnet_id=nets.subnets[net].id, ip_address=addr)],
        allowed_address_pairs=[openstack.networking.PortAllowedAddressPairArgs(ip_address=a)
                               for a in pairs] or None,
        device_owner=device_owner, tags=list(tags), opts=opts)


class GatewayPair(_Hosts):
    """gw-a and gw-b, apart, with the VIP as an allowed address on both edge ports, and the VIP's
    own port (no device) carrying the public floating IP."""

    role, flavor = "gateways", "gateway"

    def __init__(self, name: str, ctx: "Context", opts: pulumi.ResourceOptions | None = None) -> None:
        vip = ctx.platform["network"]["vip"]
        super().__init__("ipo:infra:GatewayPair", name, ["gw-a", "gw-b"], ctx, anti_affinity=True,
                         extra_pairs={"edge": [vip]}, opts=opts)
        child = pulumi.ResourceOptions(parent=self)
        self.vip_port = port(f"{ctx.prefix}vip-edge", ctx.nets, ctx.sgs, "edge", vip, "sg-gateway.edge",
                             child, tags=["role=vip"])
        self.fip: openstack.networking.FloatingIp | None = None
        if ctx.floating_ips:
            self.fip = openstack.networking.FloatingIp(
                f"{ctx.prefix}fip-public", pool=ctx.platform["network"]["external_network"],
                description="public address of the VIP", opts=child)
            openstack.networking.FloatingIpAssociate(
                f"{ctx.prefix}fip-public-vip", floating_ip=self.fip.address, port_id=self.vip_port.id,
                opts=child)


class ControlPlane(_Hosts):
    role, flavor = "control_plane", "control_plane"

    def __init__(self, name: str, ctx: "Context", opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__("ipo:infra:ControlPlane", name, ["cp-1", "cp-2"], ctx, anti_affinity=True,
                         opts=opts)


class Relay(_Hosts):
    role, flavor = "relays", "relay"

    def __init__(self, name: str, ctx: "Context", opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__("ipo:infra:Relay", name, ["relay"], ctx, anti_affinity=False, opts=opts)


class Bastion(_Hosts):
    """The only way in for operators: WireGuard on a floating IP."""

    role, flavor = "bastions", "bastion"

    def __init__(self, name: str, ctx: "Context", opts: pulumi.ResourceOptions | None = None) -> None:
        super().__init__("ipo:infra:Bastion", name, ["bastion"], ctx, anti_affinity=False, opts=opts)
        child = pulumi.ResourceOptions(parent=self)
        self.fip: openstack.networking.FloatingIp | None = None
        if ctx.floating_ips:
            self.fip = openstack.networking.FloatingIp(
                f"{ctx.prefix}fip-bastion", pool=ctx.platform["network"]["external_network"],
                description="WireGuard", opts=child)
            openstack.networking.FloatingIpAssociate(
                f"{ctx.prefix}fip-bastion-port", floating_ip=self.fip.address,
                port_id=self.ports["bastion"]["mgmt"].id, opts=child)


@dataclass(frozen=True)
class Context:
    platform: dict[str, Any]
    groups: dict[str, Any]
    nets: Networks
    sgs: SecurityGroups
    key_pair: "pulumi.Input[str]"
    prefix: str
    floating_ips: bool = True
    affinity: str = "soft-anti-affinity"


class Built:
    def __init__(self, platform: dict[str, Any], nets: Networks, sgs: SecurityGroups,
                 gateways: GatewayPair, control: ControlPlane, relay: Relay, bastion: Bastion) -> None:
        self.platform, self.nets, self.sgs = platform, nets, sgs
        self.gateways, self.control, self.relay, self.bastion = gateways, control, relay, bastion

    def outputs(self) -> dict[str, Any]:
        """What `pulumi stack output --json > outputs/<stack>.json` gives Ansible."""
        hosts = (self.gateways, self.control, self.relay, self.bastion)
        return {
            "vip": self.platform["network"]["vip"],
            "floating_ip": self.gateways.fip.address if self.gateways.fip else None,
            "bastion_floating_ip": self.bastion.fip.address if self.bastion.fip else None,
            "vip_port_id": self.gateways.vip_port.id,
            "hosts": self.platform["network"]["hosts"],
            "port_ids": {h: {n: p.id for n, p in ports.items()}
                         for c in hosts for h, ports in c.ports.items()},
            "security_groups": {name: g.id for name, g in self.sgs.groups.items()},
        }


def build(platform: dict[str, Any], groups: dict[str, Any], *, key_pair: str, prefix: str = "",
          public_key: str = "", floating_ips: bool = True,
          affinity: str = "soft-anti-affinity") -> Built:
    """`public_key`, when given, is uploaded as the Nova key pair named `key_pair`; otherwise the
    key pair must already exist in the project. Without `floating_ips` (the external network has
    no free address, say) the platform is built without its two public addresses. `affinity` is the
    policy of the gateway and control-plane server groups: `anti-affinity` refuses to boot a VM
    when no other host is free (FelCloud's project failed that way, so soft is the default),
    `soft-anti-affinity` separates them when it can."""
    nets = Networks("networks", platform, prefix)
    sgs = SecurityGroups("security-groups", groups, platform, prefix)
    key: pulumi.Input[str] = key_pair
    if public_key:
        key = openstack.compute.Keypair(f"{prefix}{key_pair}", name=f"{prefix}{key_pair}",
                                        public_key=public_key).name
    ctx = Context(platform, groups, nets, sgs, key, prefix, floating_ips, affinity)
    return Built(platform, nets, sgs, GatewayPair("gateways", ctx), ControlPlane("control-plane", ctx),
                 Relay("relay", ctx), Bastion("bastion", ctx))

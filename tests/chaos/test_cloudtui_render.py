"""The cloud-building screen, drawn from made-up state: no cloud, no Pulumi."""

import io

from rich.console import Console

from chaos import cloudtui as ct
from chaos.cloud import Res, Run, Server, Snapshot

NOW = 1000.0


def res(kind: str, name: str, parent: str, status: str, age: float = 5.0) -> Res:
    typ = f"openstack:networking/x:{kind}"
    r = Res(f"urn:{parent}:{name}", typ, name, parent, "create", status, NOW - age - 2,
            NOW - age if status != "working" else 0.0)
    return r


def sample(verb: str = "up", phase: str = "run") -> ct.CloudDash:
    d = ct.CloudDash(verb=verb, phase=phase, project="Default_Project_skan22", region="North-Africa",
                     run=Run(verb))
    d.run.started = NOW - 61
    plan = {"Networks": 11, "SecurityGroups": 47, "GatewayPair": 17, "ControlPlane": 12, "Relay": 6,
            "Bastion": 6, "": 1}
    d.plan, d.planned = plan, sum(plan.values())
    for i in range(11):
        d.run.resources[f"n{i}"] = res("Network" if i < 3 else "Subnet", f"net-{i}", "Networks", "done", 40)
    for i in range(47):
        done = i < 40
        d.run.resources[f"s{i}"] = res("SecGroupRule", f"sg-control.mgmt-from-gw-{i}-tcp-80",
                                       "SecurityGroups", "done" if done else "working", 3 if done else 0)
    for i in range(9):
        d.run.resources[f"g{i}"] = res("Port", f"gw-a-port-{i}", "GatewayPair", "done", 20 - i)
    d.run.resources["g9"] = res("Instance", "gw-a", "GatewayPair", "working")
    d.run.resources["g10"] = res("Instance", "gw-b", "GatewayPair", "working")
    d.run.resources["g11"] = res("Port", "gw-b-edge", "GatewayPair", "done", 0.2)
    d.snap = Snapshot(NOW, "Default_Project_skan22", "North-Africa", [
        Server("1", "bastion", "ACTIVE", "G0.basic.1c1g", ["10.30.0.10"]),
        Server("2", "gw-a", "BUILD", "G0.basic.1c2g", ["10.0.0.11", "10.20.0.2", "10.30.0.11"]),
        Server("3", "gw-b", "BUILD", "G0.basic.1c2g", ["10.0.0.12"])],
        {"servers": 3, "networks": 3, "routers": 2, "ports": 14, "security groups": 9, "floating ips": 0},
        [], {"instances": (3, 50), "cores": (3, 128), "ram GB": (5, 256)})
    d.floating_ips = False
    d.hosts = ct.platform_hosts()
    assert d.snap
    d.snap.networks = {"edge-net", "sandbox-net", "mgmt-net", "r-edge", "r-mgmt"}
    d.note("info", "plan: 99 resources to create, nothing to change or delete")
    d.note("warn", "floating IPs: Unable to find any IP address on external network - building without")
    d.note("dim", "CrossGuard ipo-guardrails: 4 policies, 0 violations")
    return d


def draw(d: ct.CloudDash, width: int = 130, height: int = 38) -> str:
    console = Console(file=io.StringIO(), width=width, height=height, force_terminal=True, record=True,
                      color_system="truecolor")
    console.print(ct.render(d, (width, height), now=NOW))
    return console.export_text()


def test_the_build_is_drawn_with_progress_and_the_real_cloud() -> None:
    text = draw(sample())
    for part in ("pulumi up", "Networks", "SecurityGroups", "GatewayPair", "creating", "created",
                 "in the real cloud", "North-Africa", "bastion", "ACTIVE", "BUILD", "instances",
                 "edge", "sandbox", "mgmt", "r-edge", "r-mgmt", "guardrails"):
        assert part in text, part


def test_destroy_and_the_waiting_states_read_correctly() -> None:
    text = draw(sample("destroy"))
    assert "pulumi destroy" in text and "deleting" in text and "teardown" in text
    ready = draw(sample(phase="ready"))
    assert "creates real, billed resources" in ready
    assert "ctrl-c" in draw(sample(phase="run")) and "continue" in draw(sample(phase="done"))


def test_it_fits_a_small_terminal() -> None:
    assert len(draw(sample(), 100, 30).splitlines()) <= 30


def test_component_rows_follow_the_plan_even_before_anything_arrives() -> None:
    d = ct.CloudDash(run=Run("up"))
    d.plan = {"Networks": 11, "Bastion": 6}
    assert ct.comp_rows(d) == [("Networks", 0, 0, 11), ("Bastion", 0, 0, 6)]

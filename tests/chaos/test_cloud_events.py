"""Pulumi's event stream into the model the screen draws, from events shaped like real ones."""

from chaos.cloud import Run, split_urn

URN = "urn:pulumi:live::ipo::ipo:infra:GatewayPair$openstack:compute/instance:Instance::gw-a"


def pre(urn: str, op: str = "create") -> dict[str, object]:
    typ = urn.split("::")[2].split("$")[-1]
    return {"resourcePreEvent": {"metadata": {"op": op, "urn": urn, "type": typ}}}


def out(urn: str) -> dict[str, object]:
    return {"resOutputsEvent": {"metadata": {"op": "create", "urn": urn}}}


def test_a_urn_is_split_into_type_name_and_owning_component() -> None:
    assert split_urn(URN) == ("openstack:compute/instance:Instance", "gw-a", "GatewayPair")
    top = "urn:pulumi:live::ipo::ipo:infra:Networks::networks"
    assert split_urn(top) == ("ipo:infra:Networks", "networks", "")
    key = "urn:pulumi:live::ipo::openstack:compute/keypair:Keypair::ipo-ops"
    assert split_urn(key)[2] == ""


def test_a_resource_goes_pending_to_working_to_done() -> None:
    run = Run("up")
    run.handle(pre(URN))
    assert run.resources[URN].status == "working" and run.counts["working"] == 1
    run.handle(out(URN))
    r = run.resources[URN]
    assert r.status == "done" and r.kind == "Instance" and r.parent == "GatewayPair"
    assert r.ended >= r.started and run.counts["done"] == 1


def test_unchanged_resources_the_stack_providers_and_our_components_are_not_shown() -> None:
    run = Run("up")
    run.handle(pre("urn:pulumi:live::ipo::ipo:infra:Networks::networks", "create"))
    run.handle(pre("urn:pulumi:live::ipo::pulumi:providers:openstack::default_5_5_2", "same"))
    run.handle(pre("urn:pulumi:live::ipo::pulumi:pulumi:Stack::ipo-live", "create"))
    run.handle(pre(URN, "same"))
    assert not run.resources


def test_an_error_on_a_resource_marks_it_failed_and_keeps_the_first_line() -> None:
    run = Run("up")
    run.handle(pre(URN))
    run.handle({"diagnosticEvent": {"severity": "error", "urn": URN,
                                    "message": "Error creating server: ERROR\nsecond line"}})
    assert run.resources[URN].status == "failed" and run.errors == ["Error creating server: ERROR"]
    run.handle(out(URN))  # a late output event must not resurrect it
    assert run.resources[URN].status == "failed"


def test_debug_noise_is_ignored_and_policy_checks_are_counted() -> None:
    run = Run("up")
    run.handle({"diagnosticEvent": {"severity": "debug", "message": "registering resource"}})
    run.handle({"policyLoadEvent": {}})
    run.handle({"policyAnalyzeSummaryEvent": {"passed": ["a", "b", "c", "d"]}})
    run.handle({"policyAnalyzeSummaryEvent": {"passed": ["a", "b", "c", "d"]}})
    run.handle({"policyViolationEvent": {"message": "no-world-ingress: opens 22"}})
    assert (run.policies, run.policy_checks, run.policy_resources) == (1, 8, 2)
    assert run.violations == ["no-world-ingress: opens 22"] and not run.errors and not run.log


def test_a_clean_exit_settles_anything_still_marked_working() -> None:
    run = Run("up")
    run.handle(pre(URN))
    run.settle()
    assert run.resources[URN].status == "done" and run.counts["working"] == 0


def test_the_cloud_view_round_trips_through_json() -> None:
    import json

    from chaos import cloudtui
    from chaos.test_cloudtui_render import sample

    d = sample()
    d.run.policy_checks, d.run.policy_resources = 348, 87
    back = cloudtui.load(json.loads(json.dumps(cloudtui.dump(
        {"project": d.project, "region": d.region, "elapsed": 76.0, "snapshot": d.snap}, d))))
    assert back.total == d.total and back.done == d.done and back.notes == d.notes
    assert back.snap is not None
    assert back.run.policy_checks == 348
    assert [s.name for s in back.snap.servers] == ["bastion", "gw-a", "gw-b"]

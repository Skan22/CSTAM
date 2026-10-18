"""Registration's probe through the gateways' agents."""

import pytest

from ipo.adapters.prober import AgentProber


class Stub:
    def __init__(self, *answers: tuple[bool, str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def probe(self, host: str) -> tuple[bool, str]:
        self.asked.append(host)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_any_gateway_serving_the_team_is_enough() -> None:
    down, up = Stub((False, "backend not answering")), Stub((True, ""))
    AgentProber({"gw-a": down, "gw-b": up}, interval=0).probe("a.x", timeout=1)
    assert down.asked == ["a.x"] and up.asked == ["a.x"]


def test_it_waits_for_a_sandbox_that_is_still_booting() -> None:
    booting = Stub((False, "502"), (False, "502"), (True, ""))
    AgentProber({"gw-a": booting}, interval=0).probe("a.x", timeout=5)
    assert len(booting.asked) == 3


def test_it_gives_up_with_every_gateways_reason() -> None:
    with pytest.raises(TimeoutError, match="gw-a.*no route.*gw-b.*unreachable"):
        AgentProber({"gw-a": Stub((False, "no route")), "gw-b": Stub((False, "unreachable"))},
                    interval=0.01).probe("a.x", timeout=0.05)

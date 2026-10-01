"""The dashboard's rendering, with no lab: states in, text out."""

import io

from rich.console import Console

from chaos import tui


def sample_dash(*, split: bool = False, down: bool = False) -> tui.Dash:
    d = tui.Dash(domain="cstam.felcloud.tn", vip="10.0.0.100")
    d.gateways = {
        "gw-a": tui.GwView("FAULT" if down else "MASTER", not down, 7, down, True, 425, "MASTER", 150),
        "gw-b": tui.GwView("MASTER" if split or down else "BACKUP", split or down, 7, False, True, 462,
                           "BACKUP", 100),
    }
    d.teams = [tui.TeamView("alpha", "alpha.cstam.felcloud.tn", "10.20.0.10", "active"),
               tui.TeamView("bravo", "bravo.cstam.felcloud.tn", "10.20.0.11", "pending")]
    d.split_brain = split
    for i in range(80):
        d.samples.append(tui.Sample(100 + i * 0.2, d.teams[i % 2].host, "ERR" if i == 40 else
                                    "200", "gw-a" if i < 40 else "gw-b", 3.0 + i % 5))
    d.log("gw-a: BACKUP → MASTER", "ok")
    d.log("POST /v1/gateways/failover", "act")
    d.prompt = None
    return d


def draw(d: tui.Dash, width: int = 120, height: int = 36) -> str:
    console = Console(file=io.StringIO(), width=width, height=height, force_terminal=True,
                      record=True, color_system="truecolor")
    console.print(tui.render(d, (width, height), now=116.0))
    return console.export_text()


def test_every_part_of_the_screen_is_drawn() -> None:
    text = draw(sample_dash())
    for part in ("gw-a", "gw-b", "MASTER", "BACKUP", "holds 10.0.0.100", "alpha.cstam.felcloud.tn",
                 "live requests through the VIP", "req/s", "events", "register", "heal"):
        assert part in text, part
    assert "SPLIT BRAIN" not in text


def test_split_brain_and_a_dead_primary_are_loud() -> None:
    assert "SPLIT BRAIN" in draw(sample_dash(split=True))
    assert "faulted" in draw(sample_dash(down=True))


def test_it_fits_a_small_terminal_and_a_prompt_replaces_the_key_help() -> None:
    d = sample_dash()
    d.prompt = ("register team", "dem")
    text = draw(d, 100, 30)
    assert "register team ›" in text and "dem" in text and "kill VM" not in text
    assert len(text.splitlines()) <= 30


def test_keys_edit_the_prompt_and_reject_bad_names() -> None:
    d = sample_dash()
    c = tui.Controller.__new__(tui.Controller)
    c.d = d
    for ch in "r":
        c.key(ch)
    assert d.prompt == ("register team", "")
    for ch in "Ab_c":  # lower-cased, and "_" typed as a character
        c.key(ch)
    assert d.prompt == ("register team", "ab_c")
    c.key("\x7f")
    c.key("\r")  # "ab_" is not a valid label
    assert d.prompt is None and d.events[-1][2].startswith("'ab_'")
    c.key("l")
    assert d.rate == tui.RATES[1]
    c.key("q")
    assert d.quit


def test_every_key_hint_fits_in_120_columns_with_the_cloud_key_added() -> None:
    d = sample_dash()
    d.cloud = {"region": "North-Africa", "servers": [{"status": "ACTIVE"}] * 6, "elapsed": 84}
    text = draw(d, 120, 36)
    for label in ("register", "delete", "unknown", "fail over", "kill VM", "split brain", "heal",
                  "rate", "cloud", "quit"):
        assert label in text, label
    assert "North-Africa · 6 VMs · built in 1:24" in text

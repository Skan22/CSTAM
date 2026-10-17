"""The demo's burst and expiry keys, driven against the real lab (what a viewer of the video sees)."""

from chaos import tui
from chaos.world import World
from lab.stack import wait_for


def messages(dash: tui.Dash) -> list[str]:
    return [text for _, _, text in dash.events]


def test_the_burst_key_registers_twelve_teams_and_the_expire_key_removes_them(world: World) -> None:
    world.cp.api("PATCH", "/v1/settings", {"pool_target": 12, "quarantine_seconds": 1})
    wait_for(lambda: world.cp.api("GET", "/v1/pool")[1]["size"] >= 12, "a warm pool", 120, 0.5)
    dash = tui.Dash()
    ctl = tui.Controller(world, dash)
    try:
        ctl.burst()
        assert any("12 teams on 12 different sandboxes" in m for m in messages(dash)), messages(dash)
        teams = [t for t in world.cp.api("GET", "/v1/teams")[1] if t["slug"].startswith("burst-")]
        assert len(teams) == 12 and {t["state"] for t in teams} == {"active"}
        ctl.expire_burst()
        wait_for(lambda: not [t for t in world.cp.api("GET", "/v1/teams")[1]
                              if t["slug"].startswith("burst-")], "the burst teams to go", 60, 0.5)
    finally:
        ctl.close()

"""Per-team traffic from real Traefik's access log, through the real agent, into the control plane."""

from typing import Any

from chaos.world import World
from lab import netns
from lab.stack import wait_for


def summary(w: World) -> dict[str, Any]:
    code, body = w.cp.api("GET", "/v1/traffic?window_minutes=15")
    assert code == 200, body
    return dict(body)


def mine(w: World) -> dict[str, Any]:
    [row] = [t for t in summary(w)["teams"] if t["host"] == w.host]
    return dict(row)


def get(w: World, path: str, host: str | None = None) -> None:
    netns.run(["curl", "-s", "-o", "/dev/null", "-m", "3", "-H", f"Host: {host or w.host}",
               f"http://{w.vip}{path}"], check=False)


def test_requests_through_the_vip_are_counted_for_the_team(pair: World) -> None:
    before = mine(pair)["requests"]
    for i in range(20):
        get(pair, f"/page/{i}?token=hunter2")
    for _ in range(3):
        get(pair, "/", host="nobody.invalid")  # not a registered host: must not be stored

    row = wait_for(lambda: (r := mine(pair))["requests"] >= before + 20 and r, "20 counted", 30)
    assert row["s2xx"] >= 20 and row["s5xx"] == 0
    assert all(t["host"] == pair.host for t in summary(pair)["teams"])


def test_recent_requests_omit_query_strings_and_client_addresses(pair: World) -> None:
    get(pair, "/needle?token=hunter2")
    def seen() -> Any:
        _, b = pair.cp.api("GET", f"/v1/traffic/recent?host={pair.host}&limit=50")
        return [r for r in b["requests"] if r["path"] == "/needle"]

    [hit] = wait_for(lambda: len(seen()) == 1 and seen(), "needle recorded", 30)
    assert hit["status"] == 200 and hit["method"] == "GET"
    assert "hunter2" not in str(pair.cp.api("GET", f"/v1/traffic/recent?host={pair.host}")[1])
    assert set(hit) == {"at", "gateway", "host", "slug", "method", "path", "status", "duration_ms"}

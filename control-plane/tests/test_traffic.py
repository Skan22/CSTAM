"""Per-team traffic: agents report counters read from Traefik's access log; the API sums them."""

import json
from typing import Any

from tests import test_api
from tests.test_api import Api

api = test_api.api  # reuse the API fixture


def host_row(host: str, requests: int = 1, s2xx: int | None = None, s4xx: int = 0, s5xx: int = 0,
             bytes_: int = 100, ms: int = 10) -> dict[str, Any]:
    return {"host": host, "requests": requests, "s2xx": requests - s4xx - s5xx if s2xx is None
            else s2xx, "s3xx": 0, "s4xx": s4xx, "s5xx": s5xx, "bytes": bytes_,
            "duration_ms_sum": ms}


def report(api: Api, gateway: str = "gw-a", hosts: list[dict[str, Any]] | None = None,
           recent: list[dict[str, Any]] | None = None, role: str = "operator") -> Any:
    return api.client.post(f"/v1/gateways/{gateway}/traffic", headers=api.h(role),
                           json={"hosts": hosts or [], "recent": recent or [], "dropped": 0})


def request(host: str, status: int = 200, path: str = "/") -> dict[str, Any]:
    return {"at": "2026-01-01T12:00:00Z", "host": host, "method": "GET", "path": path,
            "status": status, "duration_ms": 12}


def team_host(api: Api, slug: str) -> str:
    r = api.register(slug)
    assert r.status_code == 202, r.text
    api.run_jobs()
    return str(r.json()["subdomain"])


def summary(api: Api, **query: Any) -> Any:
    r = api.client.get("/v1/traffic", params=query, headers=api.h("viewer"))
    assert r.status_code == 200, r.text
    return r.json()


def test_counts_are_summed_per_team_across_gateways(api: Api) -> None:
    alpha, beta = team_host(api, "alpha"), team_host(api, "beta")
    assert report(api, "gw-a", [host_row(alpha, 10, s4xx=2, s5xx=1, bytes_=1000, ms=100)]
                  ).status_code == 204
    assert report(api, "gw-b", [host_row(alpha, 5), host_row(beta, 1)]).status_code == 204
    assert report(api, "gw-a", [host_row(alpha, 5)]).status_code == 204  # same minute: adds up
    body = summary(api)
    teams = {t["slug"]: t for t in body["teams"]}
    a = teams["alpha"]
    assert (a["requests"], a["s4xx"], a["s5xx"], a["s2xx"]) == (20, 2, 1, 17)
    assert a["bytes"] == 1000 + 100 + 100 and a["host"] == alpha
    assert a["avg_ms"] == (100 + 10 + 10) / 20
    assert teams["beta"]["requests"] == 1
    assert body["totals"]["requests"] == 21
    assert [t["slug"] for t in body["teams"]] == ["alpha", "beta"]  # busiest first
    assert sum(p["requests"] for p in body["series"]) == 21


def test_a_team_with_no_traffic_is_listed_with_zeros(api: Api) -> None:
    team_host(api, "quiet")
    [t] = summary(api)["teams"]
    assert (t["slug"], t["requests"], t["avg_ms"], t["last_seen"]) == ("quiet", 0, 0, None)


def test_hosts_that_are_not_routed_are_ignored(api: Api) -> None:
    alpha = team_host(api, "alpha")
    report(api, hosts=[host_row("scanner.example.net", 999), host_row(alpha.upper(), 2)],
           recent=[request("scanner.example.net"), request(alpha)])
    body = summary(api)
    assert body["totals"]["requests"] == 2
    with api.connect() as c:
        assert c.execute("SELECT count(*) FROM traffic_buckets").fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM traffic_recent").fetchone() == (1,)


def test_the_window_bounds_what_is_summed(api: Api) -> None:
    alpha = team_host(api, "alpha")
    report(api, hosts=[host_row(alpha, 4)])
    with api.connect() as c:
        c.execute("INSERT INTO traffic_buckets (bucket, gateway, host, requests, s2xx, s3xx, s4xx,"
                  " s5xx, bytes, duration_ms)"
                  " VALUES (date_trunc('minute', now()) - interval '3 hours', 'gw-a', %s,"
                  " 100, 100, 0, 0, 0, 0, 0)", (alpha,))
    assert summary(api, window_minutes=60)["totals"]["requests"] == 4
    assert summary(api, window_minutes=1440)["totals"]["requests"] == 104
    assert api.client.get("/v1/traffic", params={"window_minutes": 0},
                          headers=api.h("viewer")).status_code == 422


def test_recent_requests_are_newest_first_and_filterable(api: Api) -> None:
    alpha, beta = team_host(api, "alpha"), team_host(api, "beta")
    report(api, recent=[request(alpha, 200, "/one"), request(beta, 500, "/two"),
                        request(alpha, 404, "/three")])
    r = api.client.get("/v1/traffic/recent", headers=api.h("viewer"))
    assert [x["path"] for x in r.json()["requests"]] == ["/three", "/two", "/one"]
    r = api.client.get("/v1/traffic/recent", params={"host": alpha, "limit": 1},
                       headers=api.h("viewer"))
    assert [x["path"] for x in r.json()["requests"]] == ["/three"]
    assert r.json()["requests"][0]["slug"] == "alpha"


def test_only_the_newest_recent_requests_are_kept(api: Api) -> None:
    alpha = team_host(api, "alpha")
    for i in range(0, 30):
        report(api, recent=[request(alpha, 200, f"/{i}") for _ in range(100)])
    with api.connect() as c:
        [(n,)] = c.execute("SELECT count(*) FROM traffic_recent").fetchall()
    assert 0 < n <= 2000


def test_reporting_needs_the_operator_role_and_a_known_gateway(api: Api) -> None:
    assert report(api, role="viewer").status_code == 403
    assert report(api, gateway="gw-zzz").status_code == 404
    assert api.client.post("/v1/gateways/gw-a/traffic", headers=api.h("operator"),
                           json={"hosts": [{"host": "x"}]}).status_code == 422
    too_many = [host_row(f"h{i}.example.net") for i in range(501)]
    assert report(api, hosts=too_many).status_code == 422


def test_a_report_announces_itself_to_the_dashboard(api: Api) -> None:
    alpha = team_host(api, "alpha")
    with api.connect() as listener:
        listener.execute("LISTEN ipo_events")
        listener.commit()
        report(api, hosts=[host_row(alpha, 3), host_row("stranger.example.net", 9)])
        seen = [json.loads(n.payload) for n in listener.notifies(timeout=0.5, stop_after=1)]
    assert seen == [{"kind": "traffic.batch", "gateway": "gw-a", "requests": 3, "hosts": [alpha]}]


def test_the_traffic_tables_skip_the_write_ahead_log(api: Api) -> None:
    with api.connect() as c:
        rows = c.execute("SELECT relname, relpersistence FROM pg_class"
                         " WHERE relname IN ('traffic_buckets', 'traffic_recent')").fetchall()
    assert sorted(rows) == [("traffic_buckets", "u"), ("traffic_recent", "u")]

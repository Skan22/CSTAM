"""Team accounts: a login that sees one team's own traffic and nothing else.

The deny-by-default tests walk the OpenAPI document, so a route added later is covered without
anyone remembering to list it.
"""

import json
import threading
import time
from typing import Any

import httpx
import jwt
import pytest

from ipo.api.app import team_events
from tests import test_api
from tests.test_api import PASSWORD, SECRET, Api, serve
from tests.test_traffic import host_row, report, request, team_host

api = test_api.api  # reuse the API fixture

STAFF = ("viewer", "operator", "admin")
TEAM_ROUTES = {"/v1/me", "/v1/me/traffic", "/v1/me/traffic/recent", "/v1/me/events"}
PUBLIC = {"/healthz", "/readyz", "/metrics", "/v1/auth/login", "/openapi.json"}


def team_id(api: Api, slug: str) -> str:
    with api.connect() as c:
        row = c.execute("SELECT id::text FROM teams WHERE slug = %s", (slug,)).fetchone()
    assert row
    return str(row[0])


def make_user(api: Api, slug: str, email: str | None = None) -> dict[str, str]:
    """Register `slug`, give it a login, and return that login's auth header."""
    email = email or f"{slug}@team.example"
    r = api.client.post(f"/v1/teams/{team_id(api, slug)}/users", headers=api.h("admin"),
                        json={"email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    r = api.client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "team"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def two_teams(api: Api) -> tuple[str, str, dict[str, str], dict[str, str]]:
    alpha, beta = team_host(api, "alpha"), team_host(api, "beta")
    return alpha, beta, make_user(api, "alpha"), make_user(api, "beta")


# --------------------------------------------------------------- accounts
def test_a_team_login_carries_its_team_in_the_token(api: Api) -> None:
    team_host(api, "alpha")
    h = make_user(api, "alpha")
    claims = jwt.decode(h["Authorization"].split()[1], SECRET, algorithms=["HS256"])
    assert claims["role"] == "team" and claims["team"] == team_id(api, "alpha")


def test_only_admins_create_team_accounts(api: Api) -> None:
    team_host(api, "alpha")
    for role in ("viewer", "operator"):
        r = api.client.post(f"/v1/teams/{team_id(api, 'alpha')}/users", headers=api.h(role),
                            json={"email": "x@team.example", "password": PASSWORD})
        assert r.status_code == 403


def test_a_team_account_cannot_replace_an_existing_login(api: Api) -> None:
    team_host(api, "alpha")
    r = api.client.post(f"/v1/teams/{team_id(api, 'alpha')}/users", headers=api.h("admin"),
                        json={"email": "admin@example.com", "password": "another long password"})
    assert r.status_code == 409
    login = api.client.post("/v1/auth/login",
                            json={"email": "admin@example.com", "password": PASSWORD})
    assert login.status_code == 200 and login.json()["role"] == "admin"


def test_creating_an_account_for_an_unknown_or_deleted_team_is_404(api: Api) -> None:
    r = api.client.post("/v1/teams/00000000-0000-0000-0000-000000000000/users",
                        headers=api.h("admin"), json={"email": "x@team.example",
                                                      "password": PASSWORD})
    assert r.status_code == 404


def test_short_passwords_are_refused(api: Api) -> None:
    team_host(api, "alpha")
    r = api.client.post(f"/v1/teams/{team_id(api, 'alpha')}/users", headers=api.h("admin"),
                        json={"email": "x@team.example", "password": "short"})
    assert r.status_code == 422


def test_the_admin_bootstrap_cannot_demote_a_team_account(api: Api) -> None:
    from ipo.api.users import create_user
    team_host(api, "alpha")
    make_user(api, "alpha")
    with api.connect() as c:
        create_user(c, "alpha@team.example", "whatever long password", "admin")
        assert c.execute("SELECT role FROM users WHERE email = 'alpha@team.example'"
                         ).fetchone() == ("team",)


def test_the_database_refuses_a_team_role_without_a_team_and_the_reverse(api: Api) -> None:
    import psycopg
    team_host(api, "alpha")
    with api.connect() as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO users (email, role, password_hash) VALUES ('a', 'team', 'x')")
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("INSERT INTO users (email, role, password_hash, team_id)"
                      " VALUES ('b', 'admin', 'x', %s)", (team_id(api, "alpha"),))


def test_a_token_whose_role_and_team_claim_disagree_is_refused(api: Api) -> None:
    team_host(api, "alpha")
    exp = time.time() + 99
    tid = team_id(api, "alpha")
    for claims in ({"sub": "a", "role": "team", "exp": exp},
                   {"sub": "a", "role": "team", "team": 7, "exp": exp},
                   {"sub": "a", "role": "viewer", "team": tid, "exp": exp}):
        token = jwt.encode(claims, SECRET, algorithm="HS256")
        r = api.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, claims


# --------------------------------------------------------------------- me
def test_me_describes_the_caller(api: Api) -> None:
    host = team_host(api, "alpha")
    me = api.client.get("/v1/me", headers=make_user(api, "alpha")).json()
    assert me["role"] == "team" and me["email"] == "alpha@team.example"
    assert me["team"]["slug"] == "alpha" and me["team"]["host"] == host
    assert set(me["team"]) == {"id", "slug", "host", "state", "created_at", "expires_at"}
    staff = api.client.get("/v1/me", headers=api.h("viewer")).json()
    assert staff["role"] == "viewer" and staff["team"] is None


# ------------------------------------------------------------ deny by default
def staff_routes(api: Api) -> list[tuple[str, str]]:
    spec = api.client.get("/openapi.json").json()
    return [(m.upper(), p) for p, ops in spec["paths"].items()
            if p not in PUBLIC | TEAM_ROUTES for m in ops]


def test_a_team_token_is_refused_by_every_staff_route(api: Api) -> None:
    team_host(api, "alpha")
    h = make_user(api, "alpha")
    routes = staff_routes(api)
    assert len(routes) >= 20
    for method, path in routes:
        p = path.replace("{team_id}", team_id(api, "alpha")).replace(
            "{job_id}", "00000000-0000-0000-0000-000000000000").replace(
            "{version}", "1").replace("{name}", "gw-a")
        r = api.client.request(method, p, headers={**h, "Idempotency-Key": "k"}, json={})
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize("path", sorted(TEAM_ROUTES - {"/v1/me"}))
@pytest.mark.parametrize("role", STAFF)
def test_staff_tokens_are_refused_by_the_team_routes(api: Api, path: str, role: str) -> None:
    r = api.client.get(path, headers=api.h(role))
    assert r.status_code == 403


def test_the_team_routes_need_a_token_at_all(api: Api) -> None:
    for path in sorted(TEAM_ROUTES):
        assert api.client.get(path).status_code == 401, path


# ---------------------------------------------------------------- traffic
def test_a_team_sees_only_its_own_traffic(api: Api) -> None:
    alpha, beta, ha, hb = two_teams(api)
    report(api, hosts=[host_row(alpha, 7, bytes_=700), host_row(beta, 3, s5xx=3)],
           recent=[request(alpha, path="/a-only"), request(beta, 500, path="/b-only")])
    a = api.client.get("/v1/me/traffic", headers=ha).json()
    assert [t["slug"] for t in a["teams"]] == ["alpha"]
    assert a["totals"]["requests"] == 7 and a["totals"]["s5xx"] == 0
    assert sum(p["requests"] for p in a["series"]) == 7
    b = api.client.get("/v1/me/traffic", headers=hb).json()
    assert [t["slug"] for t in b["teams"]] == ["beta"] and b["totals"]["s5xx"] == 3
    ra = api.client.get("/v1/me/traffic/recent", headers=ha).json()["requests"]
    assert [r["path"] for r in ra] == ["/a-only"] and {r["host"] for r in ra} == {alpha}
    assert "b-only" not in json.dumps(api.client.get("/v1/me/traffic/recent", headers=ha).json())
    assert beta not in json.dumps(a)


def test_the_team_recent_route_ignores_a_host_parameter(api: Api) -> None:
    alpha, beta, ha, _ = two_teams(api)
    report(api, recent=[request(beta, path="/b-only")])
    r = api.client.get("/v1/me/traffic/recent", params={"host": beta}, headers=ha)
    assert r.status_code == 200 and r.json()["requests"] == []


def test_a_team_with_no_route_yet_sees_an_empty_summary(api: Api) -> None:
    r = api.register("pending-one")
    assert r.status_code == 202  # not run: no route exists
    h = make_user(api, "pending-one")
    body = api.client.get("/v1/me/traffic", headers=h).json()
    assert body["teams"] == [] and body["totals"]["requests"] == 0


# ----------------------------------------------------------------- events
def notes(*events: dict[str, Any]) -> list[str | None]:
    keep = team_events("T1", "alpha.example.net")
    return [keep(json.dumps(e)) for e in events]


def test_the_team_event_filter_passes_only_its_own_events() -> None:
    out = notes(
        {"kind": "team.active", "team_id": "T1", "slug": "alpha", "ip": "10.0.0.5", "host": "h"},
        {"kind": "team.active", "team_id": "T2", "slug": "beta"},
        {"kind": "team.failed", "team_id": "T1", "slug": "alpha"},
        {"kind": "alert.vm_gone", "team_id": "T1", "ip": "10.0.0.5"},
        {"kind": "gateway.split_brain", "masters": ["gw-a", "gw-b"]},
        {"kind": "gateway.vrrp", "gateway": "gw-a", "to": "MASTER"},
        {"kind": "traffic.batch", "gateway": "gw-a", "requests": 9,
         "hosts": ["alpha.example.net", "beta.example.net"]},
        {"kind": "traffic.batch", "gateway": "gw-a", "requests": 2, "hosts": ["beta.example.net"]},
    )
    assert [json.loads(o) if o else None for o in out] == [
        {"kind": "team.active", "slug": "alpha"}, None,
        {"kind": "team.failed", "slug": "alpha"}, None, None, None,
        {"kind": "traffic.batch"}, None]
    assert not any(o and ("10.0.0.5" in o or "gw-a" in o or "beta" in o) for o in out)
    assert team_events("T1", "h")("not json") is None


def test_the_team_stream_delivers_its_events_and_hides_the_rest(api: Api) -> None:
    host = team_host(api, "alpha")
    h = make_user(api, "alpha")
    tid = team_id(api, "alpha")
    seen: list[str] = []
    ready, done = threading.Event(), threading.Event()
    with serve(api) as base:
        def consume() -> None:
            with httpx.stream("GET", f"{base}/v1/me/events", headers=h, timeout=10) as r:
                assert r.status_code == 200
                for line in r.iter_lines():
                    if line.startswith(": connected"):
                        ready.set()
                    if line.startswith("data:"):
                        seen.append(line[5:])
                        if "traffic.batch" in line:
                            done.set()
                            return

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        assert ready.wait(5)
        with api.connect() as c:
            for e in ({"kind": "gateway.split_brain", "masters": ["gw-a", "gw-b"]},
                      {"kind": "team.active", "team_id": "someone-else", "slug": "beta"},
                      {"kind": "team.draining", "team_id": tid, "slug": "alpha"},
                      {"kind": "traffic.batch", "gateway": "gw-a", "requests": 1,
                       "hosts": [host]}):
                c.execute("SELECT pg_notify('ipo_events', %s)", (json.dumps(e),))
        assert done.wait(5)
        t.join(3)
    assert [json.loads(s) for s in seen] == [
        {"kind": "team.draining", "slug": "alpha"}, {"kind": "traffic.batch"}]


def test_a_team_stream_accepts_the_token_in_the_query_for_event_source(api: Api) -> None:
    team_host(api, "alpha")
    token = make_user(api, "alpha")["Authorization"].split()[1]
    with serve(api) as base:
        with httpx.stream("GET", f"{base}/v1/me/events?access_token={token}", timeout=10) as r:
            assert r.status_code == 200
        with httpx.stream("GET", f"{base}/v1/events?access_token={token}", timeout=10) as r:
            assert r.status_code == 403  # a team token does not open the staff stream

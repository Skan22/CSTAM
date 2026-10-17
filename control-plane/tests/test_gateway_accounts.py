"""A gateway agent's account is narrower than an operator's: its own gateway's heartbeat, traffic
report and config pull, and nothing else. A compromised gateway can therefore not register teams,
read the audit log, change settings, or act as another gateway."""

import pytest

from ipo.api.users import create_user
from tests import test_api
from tests.test_api import PASSWORD, Api, _fill, _protected_routes
from tests.test_team_access import make_user
from tests.test_traffic import team_host

api = test_api.api

BEAT = {"vrrp_state": "BACKUP", "live_version": 0}
TRAFFIC = {"hosts": [], "recent": [], "dropped": 0}
AGENT_ROUTES = {("POST", "/v1/gateways/{name}/heartbeat"), ("POST", "/v1/gateways/{name}/traffic"),
                ("GET", "/v1/config/latest")}


def agent_headers(api: Api, gateway: str) -> dict[str, str]:
    email = f"agent-{gateway}@example.com"
    with api.connect() as c:
        create_user(c, email, PASSWORD, "gateway", gateway)
    r = api.client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "gateway", r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_an_agent_reaches_its_own_gateways_routes(api: Api) -> None:
    h = agent_headers(api, "gw-a")
    assert api.client.post("/v1/gateways/gw-a/heartbeat", json=BEAT, headers=h).status_code == 204
    assert api.client.post("/v1/gateways/gw-a/traffic", json=TRAFFIC, headers=h).status_code == 204
    assert api.client.get("/v1/config/latest", headers=h).status_code in (200, 404)  # not 401/403


def test_an_agent_cannot_speak_for_the_other_gateway(api: Api) -> None:
    h = agent_headers(api, "gw-a")
    for path, body in (("heartbeat", BEAT), ("traffic", TRAFFIC)):
        r = api.client.post(f"/v1/gateways/gw-b/{path}", json=body, headers=h)
        assert r.status_code == 403 and "gw-a" in r.json()["detail"]


def test_an_agent_is_refused_on_every_other_route(api: Api) -> None:
    """Walks the OpenAPI document, so a route added later is covered without anyone listing it."""
    h = agent_headers(api, "gw-a")
    team_host(api, "alpha")
    allowed = {(m, p) for m, p in _protected_routes(api.client)
               if (m, p) in AGENT_ROUTES}
    assert allowed == AGENT_ROUTES
    for method, path in _protected_routes(api.client):
        if (method, path) in AGENT_ROUTES:
            continue
        r = api.client.request(method, _fill(path), headers=h, json={})
        assert r.status_code == 403, (method, path, r.status_code, r.text)


def test_staff_and_teams_are_still_governed_as_before(api: Api) -> None:
    assert api.client.post("/v1/gateways/gw-a/heartbeat", json=BEAT,
                           headers=api.h("operator")).status_code == 204
    assert api.client.post("/v1/gateways/gw-a/heartbeat", json=BEAT,
                           headers=api.h("viewer")).status_code == 403
    team_host(api, "alpha")
    mine = make_user(api, "alpha")
    beat = api.client.post("/v1/gateways/gw-a/heartbeat", json=BEAT, headers=mine)
    assert beat.status_code == 403
    assert api.client.get("/v1/config/latest", headers=mine).status_code == 403


def test_removing_an_agent_account_ends_its_tokens_at_once(api: Api) -> None:
    h = agent_headers(api, "gw-a")
    with api.connect() as c:
        c.execute("DELETE FROM users WHERE email = 'agent-gw-a@example.com'")
    assert api.client.post("/v1/gateways/gw-a/heartbeat", json=BEAT, headers=h).status_code == 401


def test_a_gateway_account_must_name_a_gateway_and_only_it_may(api: Api) -> None:
    with api.connect() as c:
        with pytest.raises(ValueError):
            create_user(c, "x@example.com", PASSWORD, "gateway")
        with pytest.raises(ValueError):
            create_user(c, "y@example.com", PASSWORD, "operator", "gw-a")

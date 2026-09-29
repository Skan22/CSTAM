import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient

from ipo.adapters.openstack.fake import FakeCloud
from ipo.api import export
from ipo.api.app import AppDeps, create_app
from ipo.api.users import create_user
from ipo.controllers.compiler import build_version
from ipo.controllers.register_saga import Deps
from ipo.controllers.workers import build_runner
from ipo.domain.signing import Signer
from ipo.settings import Settings
from tests.helpers import FakeGateways, FakeProber, make_env

SECRET = "s" * 40
PASSWORD = "correct horse battery"


@dataclass
class Api:
    client: TestClient
    connect: Callable[[], psycopg.Connection]
    settings: Settings
    cloud: FakeCloud
    failovers: list[str]
    tokens: dict[str, str]

    def h(self, role: str = "admin", **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[role]}", **extra}

    def register(self, slug: str, key: str | None = None, role: str = "operator") -> Any:
        return self.client.post("/v1/teams", json={"slug": slug}, headers=self.h(
            role, **{"Idempotency-Key": key or f"key-{slug}"}))

    def run_jobs(self, n: int = 1) -> None:
        runner = build_runner(
            self.connect, Deps(self.cloud, FakeGateways(), FakeProber(), self.settings),
            worker_id="t", sleep=lambda s: None)
        for _ in range(n):
            assert runner.run_once() == "succeeded"


def build_api(dsn: str, **env: Any) -> Api:
    settings, cloud, connect = make_env(dsn, **env)
    with connect() as c:
        for role in ("viewer", "operator", "admin"):
            create_user(c, f"{role}@example.com", PASSWORD, role)
    failovers: list[str] = []
    app = create_app(AppDeps(connect, settings, jwt_secret=SECRET, failover=failovers.append))
    client = TestClient(app, raise_server_exceptions=False)
    tokens = {}
    for role in ("viewer", "operator", "admin"):
        r = client.post("/v1/auth/login", json={"email": f"{role}@example.com",
                                                "password": PASSWORD})
        assert r.status_code == 200, r.text
        tokens[role] = r.json()["access_token"]
    return Api(client, connect, settings, cloud, failovers, tokens)


@pytest.fixture
def api(dsn: str) -> Api:
    return build_api(dsn, pooled=3)


# ------------------------------------------------------------------- auth
def test_login_issues_a_short_lived_token_with_the_role(api: Api) -> None:
    r = api.client.post("/v1/auth/login", json={"email": "operator@example.com",
                                                "password": PASSWORD})
    body = r.json()
    claims = jwt.decode(body["access_token"], SECRET, algorithms=["HS256"])
    assert body["role"] == "operator" and body["token_type"] == "bearer"
    assert claims["sub"] == "operator@example.com" and claims["role"] == "operator"
    assert 0 < claims["exp"] - time.time() <= 900 and body["expires_in"] == 900


def test_bad_credentials_are_indistinguishable(api: Api) -> None:
    wrong = api.client.post("/v1/auth/login", json={"email": "admin@example.com",
                                                    "password": "nope"})
    unknown = api.client.post("/v1/auth/login", json={"email": "ghost@example.com",
                                                      "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert wrong.headers["content-type"] == "application/problem+json"


def test_passwords_are_stored_as_argon2id(api: Api) -> None:
    with api.connect() as c:
        rows = c.execute("SELECT password_hash FROM users").fetchall()
    assert rows and all(h.startswith("$argon2id$") for (h,) in rows)


def _protected_routes(client: TestClient) -> list[tuple[str, str]]:
    spec = client.get("/openapi.json").json()
    public = {"/healthz", "/readyz", "/metrics", "/v1/auth/login", "/openapi.json"}
    return [(m.upper(), p) for p, ops in spec["paths"].items() if p not in public for m in ops]


def _fill(path: str) -> str:
    return path.replace("{team_id}", "00000000-0000-0000-0000-000000000000").replace(
        "{job_id}", "00000000-0000-0000-0000-000000000000").replace("{version}", "1")


def test_every_route_rejects_requests_without_a_valid_token(api: Api) -> None:
    routes = _protected_routes(api.client)
    assert len(routes) >= 15
    expired = jwt.encode({"sub": "admin@example.com", "role": "admin", "exp": time.time() - 5},
                         SECRET, algorithm="HS256")
    forged = jwt.encode({"sub": "admin@example.com", "role": "admin", "exp": time.time() + 99},
                        "x" * 40, algorithm="HS256")
    for method, path in routes:
        for headers in ({}, {"Authorization": f"Bearer {expired}"},
                        {"Authorization": f"Bearer {forged}"}, {"Authorization": "Bearer junk"}):
            r = api.client.request(method, _fill(path), headers=headers, json={})
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.headers["content-type"] == "application/problem+json"


def test_a_token_claiming_no_role_or_the_none_algorithm_is_refused(api: Api) -> None:
    for token in (jwt.encode({"sub": "a", "exp": time.time() + 99}, SECRET, algorithm="HS256"),
                  jwt.encode({"sub": "a", "role": "admin", "exp": time.time() + 99}, "",
                             algorithm="none")):
        r = api.client.get("/v1/teams", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


ROLE_MATRIX = [
    # method, path, body, lowest role allowed
    ("GET", "/v1/teams", None, "viewer"),
    ("GET", "/v1/pool", None, "viewer"),
    ("GET", "/v1/ipam", None, "viewer"),
    ("GET", "/v1/gateways", None, "viewer"),
    ("POST", "/v1/teams", {"slug": "matrix"}, "operator"),
    ("POST", "/v1/gateways/failover", {}, "admin"),
    ("POST", "/v1/gateways/gw-a/heartbeat", {"vrrp_state": "MASTER", "live_version": 0},
     "operator"),
    ("GET", "/v1/config/latest", None, "operator"),
    ("GET", "/v1/config/versions", None, "admin"),
    ("POST", "/v1/config/rollback", {"version": 999}, "admin"),
    ("GET", "/v1/settings", None, "admin"),
    ("PATCH", "/v1/settings", {"pool_target": 3}, "admin"),
    ("GET", "/v1/audit", None, "admin"),
]
ORDER = ["viewer", "operator", "admin"]


@pytest.mark.parametrize("method,path,body,lowest", ROLE_MATRIX,
                         ids=[f"{m} {p}" for m, p, _, _ in ROLE_MATRIX])
def test_role_matrix(api: Api, method: str, path: str, body: Any, lowest: str) -> None:
    for role in ORDER:
        headers = api.h(role, **{"Idempotency-Key": f"k-{role}"})
        r = api.client.request(method, path, json=body, headers=headers)
        allowed = ORDER.index(role) >= ORDER.index(lowest)
        if allowed:
            assert r.status_code != 403, (role, r.text)
        else:
            assert r.status_code == 403, (role, r.status_code, r.text)
            assert r.headers["content-type"] == "application/problem+json"


# ------------------------------------------------------------------ teams
def test_registering_a_team_is_accepted_with_a_job(api: Api) -> None:
    r = api.register("alpha")
    assert r.status_code == 202
    body = r.json()
    assert body["subdomain"] == f"alpha.{api.settings.domain}" and body["path"] == "warm"
    assert body["ip"].startswith("10.20.0.")
    job = api.client.get(f"/v1/jobs/{body['job_id']}", headers=api.h("viewer")).json()
    assert job["state"] == "queued" and job["kind"] == "register_team"
    assert [s["step"] for s in job["steps"]] == ["claim"]


def test_idempotency_key_is_required(api: Api) -> None:
    r = api.client.post("/v1/teams", json={"slug": "alpha"}, headers=api.h("operator"))
    assert r.status_code == 400 and "Idempotency-Key" in r.json()["detail"]


def test_replaying_the_same_key_returns_the_same_job_and_no_second_team(api: Api) -> None:
    first = api.register("alpha", "k1").json()
    second = api.register("alpha", "k1")
    assert second.status_code == 202 and second.headers["Idempotent-Replayed"] == "true"
    assert second.json()["job_id"] == first["job_id"] and second.json()["ip"] == first["ip"]
    with api.connect() as c:
        assert c.execute("SELECT count(*) FROM teams").fetchone() == (1,)


def test_conflicts_and_bad_input_are_problem_documents(api: Api) -> None:
    api.register("alpha", "k1")
    reuse = api.register("beta", "k1")
    taken = api.register("alpha", "k2")
    bad = api.register("Not_A_Slug", "k3")
    for r, status in ((reuse, 409), (taken, 409), (bad, 422)):
        assert r.status_code == status, r.text
        assert r.headers["content-type"] == "application/problem+json"
        assert {"type", "title", "status", "detail"} <= r.json().keys()


def test_idempotency_keys_are_scoped_to_the_caller(api: Api) -> None:
    a = api.register("alpha", "shared", role="operator")
    b = api.register("beta", "shared", role="admin")  # same key, different caller
    assert a.status_code == b.status_code == 202 and a.json()["job_id"] != b.json()["job_id"]


def test_the_quota_guard_and_an_empty_pool_have_distinct_errors(dsn: str) -> None:
    guarded = build_api(dsn, addresses=4, reserve_free_ips=3)
    assert guarded.register("a").status_code == 202  # 4 available, 3 left = the reserve
    r = guarded.register("b")
    assert r.status_code == 429 and r.json()["type"].endswith("quota-guard")


def test_an_exhausted_pool_is_a_503_with_retry_after(dsn: str) -> None:
    tiny = build_api(dsn, addresses=1)
    assert tiny.register("a").status_code == 202
    r = tiny.register("b")
    assert r.status_code == 503 and r.headers["Retry-After"] and r.json()["type"].endswith(
        "pool-exhausted")


def test_list_and_get_teams(api: Api) -> None:
    created = api.register("alpha").json()
    api.register("beta")
    teams = api.client.get("/v1/teams", headers=api.h("viewer")).json()
    assert [t["slug"] for t in teams] == ["alpha", "beta"]
    one = api.client.get(f"/v1/teams/{created['team_id']}", headers=api.h("viewer")).json()
    assert one["ip"] == created["ip"] and one["state"] == "pending" and one["expires_at"]
    assert api.client.get("/v1/teams?state=active", headers=api.h("viewer")).json() == []
    missing = api.client.get("/v1/teams/00000000-0000-0000-0000-000000000000",
                             headers=api.h("viewer"))
    assert missing.status_code == 404 and missing.headers["content-type"].startswith(
        "application/problem+json")
    assert api.client.get("/v1/teams/not-a-uuid", headers=api.h("viewer")).status_code == 422


def test_a_registered_team_becomes_active_and_shows_its_saga(api: Api) -> None:
    created = api.register("alpha").json()
    api.run_jobs()
    team = api.client.get(f"/v1/teams/{created['team_id']}", headers=api.h("viewer")).json()
    job = api.client.get(f"/v1/jobs/{created['job_id']}", headers=api.h("viewer")).json()
    assert team["state"] == "active" and job["state"] == "succeeded"
    assert [s["step"] for s in job["steps"]][0] == "claim" and len(job["steps"]) == 7


def test_delete_starts_a_teardown_and_is_idempotent(api: Api) -> None:
    created = api.register("alpha").json()
    api.run_jobs()
    r = api.client.delete(f"/v1/teams/{created['team_id']}", headers=api.h("operator"))
    again = api.client.delete(f"/v1/teams/{created['team_id']}", headers=api.h("operator"))
    assert r.status_code == again.status_code == 202
    assert r.json()["job_id"] == again.json()["job_id"]
    api.run_jobs()
    done = api.client.get(f"/v1/teams/{created['team_id']}", headers=api.h("viewer")).json()
    assert done["state"] == "deleted"
    assert api.client.get("/v1/teams", headers=api.h("viewer")).json() == []
    assert len(api.client.get("/v1/teams?include_deleted=true",
                              headers=api.h("viewer")).json()) == 1
    final = api.client.delete(f"/v1/teams/{created['team_id']}", headers=api.h("operator"))
    assert final.status_code == 200 and final.json()["state"] == "deleted"


def test_delete_of_a_team_still_registering_or_unknown(api: Api) -> None:
    created = api.register("alpha").json()
    busy = api.client.delete(f"/v1/teams/{created['team_id']}", headers=api.h("operator"))
    assert busy.status_code == 409
    gone = api.client.delete("/v1/teams/00000000-0000-0000-0000-000000000000",
                             headers=api.h("operator"))
    assert gone.status_code == 404


def test_extend_pushes_the_lease_and_the_team_expiry(api: Api) -> None:
    created = api.register("alpha").json()
    api.run_jobs()
    url = f"/v1/teams/{created['team_id']}/extend"
    before = api.client.get(f"/v1/teams/{created['team_id']}", headers=api.h("viewer")).json()
    r = api.client.post(url, json={"seconds": 600}, headers=api.h("operator"))
    assert r.status_code == 200
    after = r.json()
    assert after["expires_at"] > before["expires_at"]
    with api.connect() as c:
        lease_exp, team_exp = c.execute(
            "SELECT l.expires_at, t.expires_at FROM leases l JOIN teams t ON t.id = l.team_id"
        ).fetchone()  # type: ignore[misc]
    assert lease_exp == team_exp
    too_long = api.client.post(url, json={"seconds": 10**7}, headers=api.h("operator"))
    assert too_long.status_code == 422
    api.client.delete(f"/v1/teams/{created['team_id']}", headers=api.h("operator"))
    assert api.client.post(url, json={"seconds": 60}, headers=api.h("operator")).status_code == 409


def test_the_request_id_becomes_the_trace_id(api: Api) -> None:
    r = api.client.post("/v1/teams", json={"slug": "alpha"}, headers=api.h(
        "operator", **{"Idempotency-Key": "k", "X-Request-ID": "trace-123"}))
    assert r.headers["X-Request-ID"] == "trace-123"
    with api.connect() as c:
        assert c.execute("SELECT trace_id FROM jobs").fetchone() == ("trace-123",)
    assert api.client.get("/v1/teams", headers=api.h("viewer")).headers["X-Request-ID"]


# ----------------------------------------------------- pool, ipam, gateways
def test_pool_and_ipam_views(api: Api) -> None:
    api.register("alpha")
    pool = api.client.get("/v1/pool", headers=api.h("viewer")).json()
    assert pool["size"] == 2 and pool["target"] == api.settings.pool_target
    assert pool["free"] == 17 and pool["quarantined"] == 0
    ipam = api.client.get("/v1/ipam", headers=api.h("viewer")).json()
    assert ipam["total"] == 20 and ipam["counts"]["leased"] == 1 and len(ipam["leases"]) == 20
    leased = next(x for x in ipam["leases"] if x["state"] == "leased")
    assert leased["team"] == "alpha" and leased["expires_at"]


def test_gateways_lists_every_configured_gateway(api: Api) -> None:
    with api.connect() as c:
        c.execute("INSERT INTO gateway_status (gateway, vrrp_state, live_version, last_heartbeat)"
                  " VALUES ('gw-a', 'MASTER', 4, now())")
    body = api.client.get("/v1/gateways", headers=api.h("viewer")).json()
    by = {g["gateway"]: g for g in body["gateways"]}
    assert by["gw-a"]["vrrp_state"] == "MASTER" and by["gw-a"]["live_version"] == 4
    assert by["gw-b"]["vrrp_state"] == "UNKNOWN" and by["gw-b"]["live_version"] is None


def test_failover_faults_the_master_only(api: Api) -> None:
    none = api.client.post("/v1/gateways/failover", json={}, headers=api.h("admin"))
    assert none.status_code == 409  # nobody is MASTER yet
    with api.connect() as c:
        c.execute("INSERT INTO gateway_status (gateway, vrrp_state) VALUES ('gw-b', 'MASTER'),"
                  " ('gw-a', 'BACKUP')")
    r = api.client.post("/v1/gateways/failover", json={}, headers=api.h("admin"))
    assert r.status_code == 202 and r.json()["gateway"] == "gw-b"
    assert api.failovers == ["gw-b"]
    with api.connect() as c:
        assert c.execute("SELECT count(*) FROM audit_log WHERE action = 'gateway.failover'"
                         ).fetchone() == (1,)


def test_failover_is_unavailable_when_no_agent_client_is_wired(dsn: str) -> None:
    settings, _, connect = make_env(dsn)
    with connect() as c:
        create_user(c, "a@example.com", PASSWORD, "admin")
        c.execute("INSERT INTO gateway_status (gateway, vrrp_state) VALUES ('gw-a', 'MASTER')")
    client = TestClient(create_app(AppDeps(connect, settings, jwt_secret=SECRET)))
    tok = client.post("/v1/auth/login", json={"email": "a@example.com",
                                              "password": PASSWORD}).json()["access_token"]
    r = client.post("/v1/gateways/failover", json={}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 501


# ---------------------------------------------------------------- config
def _two_versions(api: Api) -> None:
    signer = Signer.generate()
    api.register("alpha")
    with api.connect() as c:
        c.execute("INSERT INTO routes (host, team_id, backend_ip, backend_port)"
                  " SELECT subdomain, id, '10.20.0.11', 80 FROM teams")
        assert build_version(c, signer, domain=api.settings.domain)
        c.execute("DELETE FROM routes")
        assert build_version(c, signer, domain=api.settings.domain)


def test_config_versions_and_rollback(api: Api) -> None:
    _two_versions(api)
    versions = api.client.get("/v1/config/versions", headers=api.h("admin")).json()
    assert [v["version"] for v in versions["versions"]] == [2, 1]
    assert versions["pinned"] is None and "body" not in versions["versions"][0]
    r = api.client.post("/v1/config/rollback", json={"version": 1}, headers=api.h("admin"))
    assert r.status_code == 202 and r.json()["pinned"] == 1
    assert api.client.get("/v1/config/versions", headers=api.h("admin")).json()["pinned"] == 1
    assert api.client.post("/v1/config/rollback", json={"version": 77},
                           headers=api.h("admin")).status_code == 404
    unpin = api.client.post("/v1/config/rollback", json={"version": None}, headers=api.h("admin"))
    assert unpin.json()["pinned"] is None


def test_one_config_version_can_be_read_with_its_body(api: Api) -> None:
    _two_versions(api)
    v = api.client.get("/v1/config/versions/1", headers=api.h("admin")).json()
    assert json.loads(v["body"])["http"]["routers"] and v["signature"]
    assert api.client.get("/v1/config/versions/9", headers=api.h("admin")).status_code == 404


def test_heartbeat_records_state_and_version_and_announces_a_change(api: Api) -> None:
    def beat(state: str, version: int) -> Any:
        return api.client.post("/v1/gateways/gw-a/heartbeat", headers=api.h("operator"),
                               json={"vrrp_state": state, "live_version": version})

    with api.connect() as listener:
        listener.execute("LISTEN ipo_events")
        listener.commit()
        assert beat("BACKUP", 3).status_code == 204
        assert beat("BACKUP", 4).status_code == 204
        assert beat("MASTER", 4).status_code == 204
        seen = [json.loads(n.payload) for n in listener.notifies(timeout=0.5, stop_after=4)]
    body = api.client.get("/v1/gateways", headers=api.h("viewer")).json()
    gw = next(g for g in body["gateways"] if g["gateway"] == "gw-a")
    assert gw["vrrp_state"] == "MASTER" and gw["live_version"] == 4 and gw["last_heartbeat"]
    # only changes are announced, not every beat: the first sighting (version and state), the
    # version moving 3 -> 4, and the state moving BACKUP -> MASTER
    assert [(e["kind"], e["to"]) for e in seen] == [
        ("gateway.config", 3), ("gateway.vrrp", "BACKUP"), ("gateway.config", 4),
        ("gateway.vrrp", "MASTER")]


def test_heartbeat_rejects_unknown_gateways_and_bad_bodies(api: Api) -> None:
    good = {"vrrp_state": "MASTER", "live_version": 1}
    r = api.client.post("/v1/gateways/gw-zzz/heartbeat", json=good, headers=api.h("operator"))
    assert r.status_code == 404
    for bad in ({"vrrp_state": "KING", "live_version": 1}, {"vrrp_state": "MASTER"},
                {"vrrp_state": "MASTER", "live_version": -1},
                {**good, "extra": 1}):
        r = api.client.post("/v1/gateways/gw-a/heartbeat", json=bad, headers=api.h("operator"))
        assert r.status_code == 422, bad


def test_latest_config_is_what_an_agent_pulls(api: Api) -> None:
    assert api.client.get("/v1/config/latest", headers=api.h("operator")).status_code == 404
    _two_versions(api)
    r = api.client.get("/v1/config/latest", headers=api.h("operator"))
    assert r.status_code == 200
    env = r.json()
    assert set(env) == {"version", "sha256", "signature", "body"} and env["version"] == 2
    with api.connect() as c:
        c.execute("UPDATE config_versions SET status = 'rejected' WHERE version = 2")
    # a version an agent refused is not offered again
    assert api.client.get("/v1/config/latest", headers=api.h("operator")).json()["version"] == 1


# -------------------------------------------------------------- settings
def test_settings_are_schema_validated_and_take_effect(api: Api) -> None:
    got = api.client.get("/v1/settings", headers=api.h("admin")).json()
    assert got["values"]["reserve_free_ips"] == api.settings.reserve_free_ips
    for bad in ({"pool_target": -1}, {"nope": 1}, {"quarantine_seconds": "soon"},
                {"lease_ttl_seconds": 5}):
        r = api.client.patch("/v1/settings", json=bad, headers=api.h("admin"))
        assert r.status_code == 422, bad
    ok = api.client.patch("/v1/settings", json={"reserve_free_ips": 100}, headers=api.h("admin"))
    assert ok.status_code == 200 and ok.json()["values"]["reserve_free_ips"] == 100
    assert api.register("alpha").status_code == 429  # the new reserve is enforced at once
    hist = api.client.get("/v1/settings", headers=api.h("admin")).json()["history"]
    assert hist[0]["actor"] == "admin@example.com" and hist[0]["detail"]["changes"] == {
        "reserve_free_ips": 100}


# ----------------------------------------------------------------- audit
def test_audit_lists_entries_and_reports_the_chain(api: Api) -> None:
    api.register("alpha")
    body = api.client.get("/v1/audit", headers=api.h("admin")).json()
    assert body["chain"] == {"valid": True, "first_bad_id": None}
    entry = body["entries"][0]
    assert entry["action"] == "team.register" and entry["actor"] == "operator@example.com"
    assert body["entries"][0]["id"] > body["entries"][-1]["id"] or len(body["entries"]) == 1
    assert api.client.get("/v1/audit?limit=1", headers=api.h("admin")).json()["entries"][
        0]["id"] == entry["id"]


# ---------------------------------------------------------- ops endpoints
def test_health_ready_and_metrics_are_open(api: Api) -> None:
    assert api.client.get("/healthz").json() == {"status": "ok"}
    assert api.client.get("/readyz").json() == {"status": "ready"}
    api.register("alpha")
    text = api.client.get("/metrics").text
    assert 'ipo_ip_addresses{state="leased"} 1.0' in text
    assert "ipo_pool_size 2.0" in text and "ipo_pool_target" in text
    assert "ipo_api_request_duration_seconds_count" in text


def test_readyz_fails_when_the_database_is_down(dsn: str) -> None:
    settings, _, _ = make_env(dsn)

    def broken() -> psycopg.Connection:
        raise psycopg.OperationalError("connection refused")

    client = TestClient(create_app(AppDeps(broken, settings, jwt_secret=SECRET)))
    assert client.get("/healthz").status_code == 200
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "not ready"


def test_unexpected_errors_are_a_generic_problem_not_a_stack_trace(dsn: str) -> None:
    settings, _, connect = make_env(dsn)

    def broken() -> psycopg.Connection:
        raise RuntimeError("secret internals")

    client = TestClient(create_app(AppDeps(broken, settings, jwt_secret=SECRET)),
                        raise_server_exceptions=False)
    r = client.post("/v1/auth/login", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 500 and "secret internals" not in r.text
    assert r.headers["content-type"] == "application/problem+json"


def test_a_short_jwt_secret_is_refused(dsn: str) -> None:
    settings, _, connect = make_env(dsn)
    with pytest.raises(ValueError, match="secret"):
        create_app(AppDeps(connect, settings, jwt_secret="short"))


# ---------------------------------------------------------------- events
@contextmanager
def serve(api: Api) -> Iterator[str]:
    """The app on a real socket: an endless stream can't be read through TestClient."""
    server = uvicorn.Server(uvicorn.Config(api.client.app, host="127.0.0.1", port=0,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(5)


def test_events_stream_delivers_notifications_within_a_second(api: Api) -> None:
    lines: list[str] = []
    ready, got = threading.Event(), threading.Event()
    with serve(api) as base:
        def consume() -> None:
            with httpx.stream("GET", f"{base}/v1/events", headers=api.h("viewer"),
                              timeout=10) as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                for line in r.iter_lines():
                    lines.append(line)
                    if line.startswith(": connected"):
                        ready.set()
                    if line.startswith("data:"):
                        got.set()
                        return

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        assert ready.wait(5)
        started = time.monotonic()
        with api.connect() as c:
            c.execute("SELECT pg_notify('ipo_events', %s)",
                      (json.dumps({"kind": "team.active"}),))
        assert got.wait(3) and time.monotonic() - started < 1.0
        t.join(3)
    data = next(line for line in lines if line.startswith("data:"))
    assert json.loads(data[5:]) == {"kind": "team.active"}
    assert "event: team.active" in lines and any(line.startswith("id: ") for line in lines)


def test_events_accept_the_token_as_a_query_parameter_for_event_source(api: Api) -> None:
    with serve(api) as base:
        with httpx.stream("GET", f"{base}/v1/events?access_token={api.tokens['viewer']}",
                          timeout=10) as r:
            assert r.status_code == 200
        with httpx.stream("GET", f"{base}/v1/events?access_token=junk", timeout=10) as bad:
            assert bad.status_code == 401
    # the query-string token is for the event stream only
    other = api.client.get(f"/v1/teams?access_token={api.tokens['viewer']}")
    assert other.status_code == 401


def test_closing_the_stream_releases_the_listening_connection(api: Api) -> None:
    def listeners() -> int:
        with api.connect() as c:
            row = c.execute("SELECT count(*) FROM pg_stat_activity"
                            " WHERE query = 'LISTEN ipo_events'").fetchone()
        assert row
        return int(row[0])

    with serve(api) as base:
        with httpx.stream("GET", f"{base}/v1/events", headers=api.h("viewer"), timeout=10) as r:
            next(r.iter_lines())
            assert listeners() == 1
        deadline = time.monotonic() + 5
        while listeners() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert listeners() == 0


# ---------------------------------------------------------------- export
DOCS = Path(__file__).resolve().parents[2] / "docs" / "api"


def test_the_committed_openapi_and_postman_files_match_the_code() -> None:
    """Regenerate with: uv run python -m ipo.api.export ../docs/api"""
    for name, text in export.render().items():
        assert (DOCS / name).read_text() == text, f"{name} is stale"


def test_the_postman_collection_covers_every_operation() -> None:
    spec = export.openapi()
    coll = export.postman(spec)
    n = sum(len(item["item"]) for item in coll["item"])
    assert n == sum(len(ops) for ops in spec["paths"].values())
    assert coll["auth"]["type"] == "bearer"
    assert "Idempotency-Key" in json.dumps(coll)


def test_every_operation_documents_its_role_and_errors() -> None:
    spec = export.openapi()
    for path, ops in spec["paths"].items():
        if path in {"/healthz", "/readyz", "/metrics", "/v1/auth/login"}:
            continue
        for op in ops.values():
            assert "401" in op["responses"] and op["security"], path


def test_a_zero_version_heartbeat_does_not_erase_the_known_version(api: Api) -> None:
    def beat(version: int) -> Any:
        return api.client.post("/v1/gateways/gw-a/heartbeat", headers=api.h("operator"),
                               json={"vrrp_state": "BACKUP", "live_version": version})

    assert beat(4).status_code == 204
    assert beat(0).status_code == 204
    body = api.client.get("/v1/gateways", headers=api.h("viewer")).json()
    assert next(g for g in body["gateways"] if g["gateway"] == "gw-a")["live_version"] == 4

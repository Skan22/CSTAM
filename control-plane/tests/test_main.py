"""The whole process, wired as in production but with the fake cloud and in-process agents."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from ipo.adapters.local_agent import LocalAgent
from ipo.adapters.openstack.fake import FakeCloud
from ipo.domain.signing import Signer
from ipo.main import Runtime, build_runtime

PLATFORM = Path(__file__).resolve().parents[2] / "platform.yaml"


def wait_for(cond: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


@pytest.fixture
def system(dsn: str) -> Iterator[tuple[Runtime, TestClient, FakeCloud, dict[str, str]]]:
    signer, cloud = Signer.generate(), FakeCloud()
    env = {"IPO_DATABASE_URL": dsn, "IPO_PLATFORM": str(PLATFORM), "IPO_CLOUD": "fake",
           "IPO_JWT_SECRET": "j" * 40, "IPO_ADMIN_EMAIL": "root@example.com",
           "IPO_ADMIN_PASSWORD": "hunter2-hunter2", "IPO_WORKERS": "2"}
    agents = {g: LocalAgent(signer.public_b64) for g in ("gw-a", "gw-b")}
    rt = build_runtime(env, cloud=cloud, signer=signer, agents=agents)
    rt.start()
    client = TestClient(rt.app)
    token = client.post("/v1/auth/login", json={"email": "root@example.com",
                                                "password": "hunter2-hunter2"}).json()
    client.headers["Authorization"] = f"Bearer {token['access_token']}"
    try:
        yield rt, client, cloud, {"dsn": dsn}
    finally:
        rt.shutdown()


def test_a_team_goes_from_registration_to_serving_and_back_to_nothing(
    system: tuple[Runtime, TestClient, FakeCloud, dict[str, str]],
) -> None:
    rt, client, cloud, _ = system
    # the pool manager fills the warm pool to its target on its own
    wait_for(lambda: client.get("/v1/pool").json()["size"] == rt.settings.pool_target)

    reg = client.post("/v1/teams", json={"slug": "alpha"}, headers={"Idempotency-Key": "k"})
    assert reg.status_code == 202, reg.text
    wait_for(lambda: client.get(f"/v1/jobs/{reg.json()['job_id']}").json()["state"] == "succeeded")
    team = client.get(f"/v1/teams/{reg.json()['team_id']}").json()
    assert team["state"] == "active"

    # the compiler published the route and both agents run a version that contains it
    versions = client.get("/v1/config/versions").json()["versions"]
    assert versions and all(g.live_version == versions[0]["version"]
                            for g in map(_agent(rt), ("gw-a", "gw-b")))
    assert f"Host(`alpha.{rt.settings.domain}`)" in _agent(rt)("gw-a").body
    assert {g["vrrp_state"] for g in client.get("/v1/gateways").json()["gateways"]}

    client.delete(f"/v1/teams/{team['id']}")
    wait_for(lambda: client.get(f"/v1/teams/{team['id']}").json()["state"] == "deleted")
    assert f"Host(`alpha.{rt.settings.domain}`)" not in _agent(rt)("gw-a").body

    # the pool manager tops the pool back up; nothing is left behind
    wait_for(lambda: client.get("/v1/pool").json()["size"] == rt.settings.pool_target)
    wait_for(lambda: len(cloud.servers) == rt.settings.pool_target)
    assert len(cloud.ports) == rt.settings.pool_target
    assert client.get("/v1/audit").json()["chain"]["valid"]


def _agent(rt: Runtime) -> Callable[[str], LocalAgent]:
    def get(name: str) -> LocalAgent:
        agent = rt.agents[name]
        assert isinstance(agent, LocalAgent)
        return agent
    return get


def test_a_bad_signature_is_refused_by_the_agent() -> None:
    signer = Signer.generate()
    agent = LocalAgent(signer.public_b64)
    from ipo.controllers.compiler import ConfigVersion
    from ipo.domain.signing import sha256_hex
    body = "{}"
    good = ConfigVersion(1, sha256_hex(body), signer.sign(1, sha256_hex(body), body), body)
    assert agent.put_config(good).ok
    assert not agent.put_config(good).ok  # replay of the same version
    forged = ConfigVersion(2, sha256_hex(body), Signer.generate().sign(2, sha256_hex(body), body),
                           body)
    assert not agent.put_config(forged).ok and agent.live_version == 1


def test_missing_required_settings_fail_loudly(dsn: str) -> None:
    with pytest.raises(KeyError):
        build_runtime({"IPO_PLATFORM": str(PLATFORM)})
    with pytest.raises(ValueError, match="secret"):
        build_runtime({"IPO_DATABASE_URL": dsn, "IPO_PLATFORM": str(PLATFORM),
                       "IPO_CLOUD": "fake", "IPO_JWT_SECRET": "short"})


def test_the_pool_is_seeded_from_platform_yaml(dsn: str) -> None:
    build_runtime({"IPO_DATABASE_URL": dsn, "IPO_PLATFORM": str(PLATFORM), "IPO_CLOUD": "fake",
                   "IPO_JWT_SECRET": "j" * 40})
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM leases").fetchone() == (100,)

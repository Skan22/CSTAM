"""Many teams registering at the same instant, through the whole process (fake cloud, in-process
agents): no failures, no shared address, no lost route, and a retry storm creates one team."""

import threading
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import httpx
import pytest
from fastapi.testclient import TestClient

from ipo.adapters.local_agent import LocalAgent
from ipo.adapters.openstack.fake import FakeCloud
from ipo.domain.signing import Signer
from ipo.main import Runtime, build_runtime
from tests.test_main import PLATFORM, wait_for

TEAMS = 15


@pytest.fixture
def system(dsn: str) -> Iterator[tuple[Runtime, TestClient, FakeCloud]]:
    signer, cloud = Signer.generate(), FakeCloud(boot_seconds=0.05)
    env = {"IPO_DATABASE_URL": dsn, "IPO_PLATFORM": str(PLATFORM), "IPO_CLOUD": "fake",
           "IPO_JWT_SECRET": "j" * 40, "IPO_ADMIN_EMAIL": "root@example.com",
           "IPO_ADMIN_PASSWORD": "hunter2-hunter2"}
    agents = {g: LocalAgent(signer.public_b64) for g in ("gw-a", "gw-b")}
    rt = build_runtime(env, cloud=cloud, signer=signer, agents=agents)
    rt.start()
    client = TestClient(rt.app)
    token = client.post("/v1/auth/login", json={"email": "root@example.com",
                                                "password": "hunter2-hunter2"}).json()
    client.headers["Authorization"] = f"Bearer {token['access_token']}"
    try:
        yield rt, client, cloud
    finally:
        rt.shutdown()


def register(client: TestClient, slug: str, key: str) -> httpx.Response:
    reply: httpx.Response = client.post("/v1/teams", json={"slug": slug},
                                        headers={"Idempotency-Key": key})
    return reply


def burst(calls: list[Callable[[], httpx.Response]]) -> list[httpx.Response]:
    """Run every call on its own thread, released together."""
    gate = threading.Barrier(len(calls))

    def go(call: Callable[[], httpx.Response]) -> httpx.Response:
        gate.wait()
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as ex:
        return list(ex.map(go, calls))


def test_simultaneous_registrations_all_succeed_with_distinct_addresses(
    system: tuple[Runtime, TestClient, FakeCloud],
) -> None:
    rt, client, cloud = system
    wait_for(lambda: client.get("/v1/pool").json()["size"] == rt.settings.pool_target)

    replies = burst([
        partial(register, client, f"team-{i:02d}", f"k{i}") for i in range(TEAMS)])
    assert [r.status_code for r in replies] == [202] * TEAMS
    regs = [r.json() for r in replies]

    ips = [r["ip"] for r in regs]
    assert len(set(ips)) == TEAMS, Counter(ips)
    assert {r["path"] for r in regs} == {"warm", "cold"}  # the pool covered some, the rest booted

    wait_for(lambda: all(client.get(f"/v1/teams/{r['team_id']}").json()["state"] == "active"
                         for r in regs), timeout=60)
    jobs = [client.get(f"/v1/jobs/{r['job_id']}").json() for r in regs]
    assert {j["state"] for j in jobs} == {"succeeded"}, [j["last_error"] for j in jobs]

    # every route is live on both gateways, each pointing at its own team's address
    for gw in ("gw-a", "gw-b"):
        agent = rt.agents[gw]
        assert isinstance(agent, LocalAgent)
        for r in regs:
            assert f"Host(`{r['subdomain']}`)" in agent.body, (gw, r["subdomain"])
            assert f"http://{r['ip']}:" in agent.body

    # no VM or port is shared or leaked: one server per leased or pooled address
    assert len({s.port_id for s in cloud.servers.values()}) == len(cloud.servers)


def test_a_retry_storm_with_one_idempotency_key_creates_one_team(
    system: tuple[Runtime, TestClient, FakeCloud],
) -> None:
    _, client, _ = system
    replies = burst([
        partial(register, client, "storm", "same") for _ in range(12)])
    codes = Counter(r.status_code for r in replies)
    assert set(codes) == {202}, codes
    assert len({r.json()["team_id"] for r in replies}) == 1
    assert len([t for t in client.get("/v1/teams").json() if t["slug"] == "storm"]) == 1


def test_competing_registrations_for_one_name_have_exactly_one_winner(
    system: tuple[Runtime, TestClient, FakeCloud],
) -> None:
    _, client, _ = system
    replies = burst([
        partial(register, client, "contested", f"c{i}") for i in range(10)])
    codes = Counter(r.status_code for r in replies)
    assert codes == {202: 1, 409: 9}, codes

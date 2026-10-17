"""What is left of a team after it is torn down: nothing that identifies, reaches or reuses it.

The saga tests elsewhere prove the order of the steps. These check the outcome of the whole
cleanup pipeline: DNS withdrawn, logins and telemetry purged, the address sanitised before it is
recycled, and every leftover repaired by the reconciler.
"""

from collections.abc import Callable

import psycopg
import pytest

from ipo import runtime_settings
from ipo.adapters.dns import DnsError, FakeDns
from ipo.adapters.openstack.base import Server, port_name, server_name
from ipo.adapters.openstack.fake import FakeCloud
from ipo.controllers.reaper import Reaper
from ipo.controllers.reconciler import Reconciler
from ipo.controllers.register_saga import Deps
from ipo.controllers.saga import SagaRunner
from ipo.controllers.workers import build_runner
from ipo.domain import leases
from ipo.domain.registration import register_team
from ipo.domain.teardown import NotSanitized, enqueue_teardown, verify_released
from ipo.runtime_settings import SettingsPatch
from ipo.settings import Settings
from tests import test_api
from tests.helpers import FakeGateways, FakeProber, make_env
from tests.test_api import PASSWORD, Api
from tests.test_team_access import make_user, team_id
from tests.test_traffic import report, request, team_host

api = test_api.api

Env = tuple[Settings, FakeCloud, Callable[[], psycopg.Connection]]


def runner(env: Env, gw: FakeGateways, dns: FakeDns) -> SagaRunner:
    settings, cloud, connect = env
    return build_runner(connect, Deps(cloud, gw, FakeProber(), settings, dns), worker_id="w",
                        sleep=lambda s: None)


def register(env: Env, gw: FakeGateways, dns: FakeDns, slug: str = "alpha") -> tuple[str, str, str]:
    settings, _, connect = env
    with connect() as c:
        reg = register_team(c, settings, slug=slug, owner="ada", idempotency_key=f"k-{slug}")
    assert runner(env, gw, dns).run_once() == "succeeded"
    return reg.team_id, reg.ip, reg.subdomain


# ------------------------------------------------------------------- dns
def test_the_dns_record_exists_exactly_while_the_team_does(dsn: str) -> None:
    env, gw, dns = make_env(dsn), FakeGateways(), FakeDns()
    team, _, host = register(env, gw, dns)
    assert dns.records == {host: dns.target}

    seen: list[bool] = []
    gw.on_removal = lambda c: seen.append(dns.exists(host))
    with env[2]() as c:
        enqueue_teardown(c, team, reason="api", actor="ada")
    assert runner(env, gw, dns).run_once() == "succeeded"
    assert seen == [True]  # still there while the gateways stop serving it, then withdrawn
    assert dns.records == {}


def test_a_dns_server_that_is_down_fails_the_registration_cleanly(dsn: str) -> None:
    env, gw, dns = make_env(dsn, pooled=1), FakeGateways(), FakeDns()
    dns.inject("ensure", DnsError("no route to the name server"), times=99)
    settings, _, connect = env
    with connect() as c:
        register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k")
    r = build_runner(connect, Deps(env[1], gw, FakeProber(), settings, dns), worker_id="w",
                     sleep=lambda s: None, max_step_attempts=2)
    assert r.run_once() == "compensated"
    assert dns.records == {} and gw.waited == []  # never advertised, never routed


def test_a_dns_server_that_is_down_delays_teardown_but_does_not_lose_it(dsn: str) -> None:
    env, gw, dns = make_env(dsn), FakeGateways(), FakeDns()
    team, ip, host = register(env, gw, dns)
    dns.inject("remove", DnsError("timeout"), times=1)
    with env[2]() as c:
        enqueue_teardown(c, team, reason="api", actor="ada")
    assert runner(env, gw, dns).run_once() == "succeeded"  # the step is retried
    assert dns.records == {}
    with env[2]() as c:
        assert leases.get(c, ip).state == "quarantined"


def test_reconciler_withdraws_stale_records_and_restores_missing_ones(dsn: str) -> None:
    env, gw, dns = make_env(dsn), FakeGateways(), FakeDns()
    team, _, host = register(env, gw, dns, "alpha")
    _, _, other = register(env, gw, dns, "beta")
    settings, cloud, connect = env
    rec = Reconciler(cloud, settings, connect, grace_seconds=0, dns=dns)
    assert rec.run_once()["stale_dns"] == 0

    dns.records["ghost.cstam.felcloud.tn"] = dns.target  # not a team: left alone
    del dns.records[other]                               # an active team lost its record
    with connect() as c:
        enqueue_teardown(c, team, reason="api", actor="ada")
    assert runner(env, gw, dns).run_once() == "succeeded"
    dns.records[host] = dns.target                       # a deleted team's record came back

    repairs = rec.run_once()
    assert repairs["stale_dns"] == 1 and repairs["missing_dns"] == 1
    assert set(dns.records) == {other, "ghost.cstam.felcloud.tn"}
    assert rec.run_once()["stale_dns"] == 0  # converged


def test_the_wildcard_default_has_nothing_to_publish_or_repair(dsn: str) -> None:
    env, gw = make_env(dsn), FakeGateways()
    settings, cloud, connect = env
    build = build_runner(connect, Deps(cloud, gw, FakeProber(), settings), worker_id="w",
                         sleep=lambda s: None)
    with connect() as c:
        register_team(c, settings, slug="alpha", owner="ada", idempotency_key="k")
    assert build.run_once() == "succeeded"
    with connect() as c:
        results = [r for (r,) in c.execute(
            "SELECT result FROM saga_steps WHERE step = 'dns'")]
    assert results == [{"managed": False}]
    assert sum(Reconciler(cloud, settings, connect, grace_seconds=0).run_once().values()) == 0


# --------------------------------------------------------- sanitization
def test_an_address_with_anything_left_on_it_is_not_released(dsn: str) -> None:
    env, gw, dns = make_env(dsn), FakeGateways(), FakeDns()
    team, ip, _ = register(env, gw, dns)
    settings, cloud, connect = env
    with connect() as c:
        enqueue_teardown(c, team, reason="api", actor="ada")
    assert runner(env, gw, dns).run_once() == "succeeded"
    with connect() as c:
        verify_released(c, cloud, ip)  # clean
        port = cloud.ensure_port(name=port_name(ip), ip=ip)
        cloud.ensure_server(name=server_name(ip), port_id=port.id)
        with pytest.raises(NotSanitized, match="server"):
            verify_released(c, cloud, ip)


def test_a_cloud_that_keeps_the_vm_makes_the_release_retry_instead_of_recycling(dsn: str) -> None:
    env, gw, dns = make_env(dsn), FakeGateways(), FakeDns()
    team, ip, _ = register(env, gw, dns)
    _, cloud, connect = env
    real_delete = cloud.delete_server
    kept = {"n": 0}

    def flaky(server_id: str) -> None:  # the API says OK but the VM is still there, once
        if kept["n"] == 0:
            kept["n"] += 1
            return
        real_delete(server_id)

    cloud.delete_server = flaky  # type: ignore[method-assign]
    with connect() as c:
        enqueue_teardown(c, team, reason="api", actor="ada")
    assert runner(env, gw, dns).run_once() == "succeeded"
    assert kept["n"] == 1 and not cloud.servers and not cloud.ports


def test_expiry_cleans_up_without_anyone_asking(dsn: str) -> None:
    env, gw, dns = make_env(dsn, quarantine_seconds=0), FakeGateways(), FakeDns()
    _, ip, host = register(env, gw, dns)
    _, cloud, connect = env
    with connect() as c:
        c.execute("UPDATE leases SET expires_at = now() - interval '1 second' WHERE ip = %s", (ip,))
    reaper = Reaper(connect)
    assert reaper.run_once().torn_down == 1
    assert runner(env, gw, dns).run_once() == "succeeded"
    assert reaper.run_once().freed == [ip]
    with connect() as c:
        lease = leases.get(c, ip)
        assert lease.state == "free" and not (lease.team_id or lease.server_id or lease.port_id)
        assert c.execute("SELECT count(*) FROM routes").fetchone() == (0,)
    assert dns.records == {} and not cloud.servers and not cloud.ports and gw.removed == [host]


# -------------------------------------------------- logins and telemetry
def test_deleting_a_team_removes_its_logins_and_traffic_and_voids_its_tokens(api: Api) -> None:
    host = team_host(api, "alpha")
    team_host(api, "beta")
    mine = make_user(api, "alpha")
    theirs = make_user(api, "beta")
    report(api, hosts=[{"host": host, "requests": 3, "s2xx": 3, "s3xx": 0, "s4xx": 0, "s5xx": 0,
                        "bytes": 10, "duration_ms": 9}], recent=[request(host)])
    assert api.client.get("/v1/me", headers=mine).status_code == 200

    r = api.client.delete(f"/v1/teams/{team_id(api, 'alpha')}", headers=api.h("operator"))
    assert r.status_code == 202
    api.run_jobs()

    assert api.client.get("/v1/me", headers=mine).status_code == 401  # token still unexpired
    assert api.client.post("/v1/auth/login", json={"email": "alpha@team.example",
                                                   "password": PASSWORD}).status_code == 401
    assert api.client.get("/v1/me", headers=theirs).status_code == 200  # other teams unaffected
    with api.connect() as c:
        assert c.execute("SELECT count(*) FROM users WHERE email = 'alpha@team.example'"
                         ).fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM traffic_buckets WHERE host = %s", (host,)
                         ).fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM traffic_recent WHERE host = %s", (host,)
                         ).fetchone() == (0,)
        assert c.execute("SELECT verify_audit_chain()").fetchone() == (None,)
        detail = c.execute("SELECT detail FROM audit_log WHERE action = 'team.deleted'"
                           ).fetchone()
    assert detail and detail[0]["purged"]["users"] == 1


def test_a_reused_name_is_refused_so_an_old_tenant_cannot_be_confused_with_a_new_one(
    api: Api,
) -> None:
    team_host(api, "alpha")
    api.client.delete(f"/v1/teams/{team_id(api, 'alpha')}", headers=api.h("operator"))
    api.run_jobs()
    assert api.register("alpha", key="again").status_code == 409


def test_the_quarantine_an_admin_sets_at_runtime_applies_to_the_next_teardown(dsn: str) -> None:
    env, gw, dns = make_env(dsn, quarantine_seconds=60), FakeGateways(), FakeDns()
    team, ip, _ = register(env, gw, dns)
    _, _, connect = env
    with connect() as c:
        patch = SettingsPatch.model_validate({"quarantine_seconds": 7})
        runtime_settings.update(c, patch, actor="admin")
        enqueue_teardown(c, team, reason="api", actor="admin")
    assert runner(env, gw, dns).run_once() == "succeeded"
    with connect() as c:
        row = c.execute("SELECT extract(epoch FROM quarantined_until - now()) FROM leases"
                        " WHERE ip = %s", (ip,)).fetchone()
    assert row and 5 < float(row[0]) <= 7


class LaggyCloud(FakeCloud):
    """Deletes like Nova: the call returns at once and the VM keeps being listed for a while."""

    def __init__(self, lag: int) -> None:
        super().__init__()
        self.lag = lag
        self._doomed: dict[str, int] = {}

    def delete_server(self, server_id: str) -> None:
        self._doomed.setdefault(server_id, self.lag)

    def list_servers(self, tag: str = "ipo") -> list[Server]:
        for sid in list(self._doomed):
            if self._doomed[sid] <= 0:
                self.servers.pop(sid, None)
                del self._doomed[sid]
            else:
                self._doomed[sid] -= 1
        return super().list_servers(tag)


@pytest.mark.parametrize(("wait", "outcome"), [(0.0, "failed"), (30.0, "succeeded")])
def test_teardown_waits_for_a_cloud_that_deletes_asynchronously(
    dsn: str, monkeypatch: pytest.MonkeyPatch, wait: float, outcome: str,
) -> None:
    """On a real cloud the VM is still listed for seconds after the delete is accepted. That is
    not a leftover: the release waits for it to go, and the address is only recycled then.
    Without the wait (what the first real-cloud run did) every teardown failed into the
    reconciler's minute-long cycle."""
    from ipo.controllers.pool import PoolManager

    monkeypatch.setattr("ipo.domain.teardown.time.sleep", lambda s: None)  # poll without waiting
    settings, _, connect = make_env(dsn, quarantine_seconds=0)
    cloud, gw, dns = LaggyCloud(lag=8), FakeGateways(), FakeDns()
    PoolManager(cloud, settings, connect).reconcile(target=1)
    team, ip, _ = register((settings, cloud, connect), gw, dns)
    with connect() as c:
        enqueue_teardown(c, team, reason="api", actor="ada")
    runner_ = build_runner(
        connect, Deps(cloud, gw, FakeProber(), settings, dns, release_verify_seconds=wait),
        worker_id="w", sleep=lambda s: None, max_job_attempts=1)
    result = runner_.run_once()
    if outcome == "succeeded":
        assert result == "succeeded"
        assert not [s for s in cloud.list_servers() if s.name == server_name(ip)]
        with connect() as c:
            assert leases.get(c, ip).state in ("quarantined", "free")
    else:
        assert result in ("failed", "compensated")  # the VM "is still there": not recycled

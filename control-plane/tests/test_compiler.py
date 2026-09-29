import json
import random
import threading
import time
from collections.abc import Callable, Mapping
from functools import partial

import psycopg
import pytest
from cryptography.exceptions import InvalidSignature

from ipo.controllers.compiler import (
    Ack,
    Agent,
    CompilerController,
    ConfigVersion,
    DbGateways,
    Pusher,
    RouteRow,
    build_version,
    render,
    rollback,
)
from ipo.domain.signing import Signer, verify
from ipo.settings import Settings

DOMAIN = "cstam.felcloud.tn"


def rows(n: int) -> list[RouteRow]:
    return [RouteRow(f"t{i}.{DOMAIN}", f"10.20.0.{10 + i}", 80, []) for i in range(n)]


def add_route(conn: psycopg.Connection, i: int, mw: str = "[]") -> None:
    team = conn.execute(
        "INSERT INTO teams (slug, subdomain, owner, expires_at)"
        " VALUES (%s, %s, 'o', now() + interval '1h') RETURNING id",
        (f"t{i}", f"t{i}.{DOMAIN}"),
    ).fetchone()
    assert team
    conn.execute(
        "INSERT INTO routes (host, team_id, backend_ip, backend_port, middlewares)"
        " VALUES (%s, %s, %s, 80, %s)", (f"t{i}.{DOMAIN}", team[0], f"10.20.0.{10 + i}", mw))


# ------------------------------------------------------------------ render
def test_render_is_byte_identical_for_the_same_state() -> None:
    a = rows(12)
    b = list(a)
    random.Random(7).shuffle(b)
    assert render(a, domain=DOMAIN) == render(b, domain=DOMAIN)


def test_render_has_one_router_and_service_per_team_plus_a_canary() -> None:
    cfg = json.loads(render(rows(3), domain=DOMAIN))
    assert len(cfg["http"]["routers"]) == 4 and len(cfg["http"]["services"]) == 4
    r = cfg["http"]["routers"]["t-t1-cstam-felcloud-tn"]
    assert r["rule"] == f"Host(`t1.{DOMAIN}`)"
    svc = cfg["http"]["services"][r["service"]]
    assert svc["loadBalancer"]["servers"] == [{"url": "http://10.20.0.11:80"}]
    assert f"Host(`canary.{DOMAIN}`)" == cfg["http"]["routers"]["ipo-canary"]["rule"]


def test_render_rejects_unknown_middlewares() -> None:
    bad = [RouteRow(f"a.{DOMAIN}", "10.20.0.10", 80, ["exec-arbitrary"])]
    with pytest.raises(ValueError, match="exec-arbitrary"):
        render(bad, domain=DOMAIN)


def test_known_middlewares_are_defined_once_and_referenced() -> None:
    r = [RouteRow(f"a.{DOMAIN}", "10.20.0.10", 80, ["secure-headers"])]
    cfg = json.loads(render(r, domain=DOMAIN))
    assert "secure-headers" in cfg["http"]["middlewares"]
    assert cfg["http"]["routers"]["t-a-cstam-felcloud-tn"]["middlewares"] == ["secure-headers"]


# ------------------------------------------------------------ build + sign
def test_build_stores_a_verifiable_signed_version(conn: psycopg.Connection) -> None:
    signer = Signer.generate()
    add_route(conn, 1)
    v = build_version(conn, signer, domain=DOMAIN, trace_id="tr-1")
    assert v and v.version == 1
    verify(signer.public_b64, v.version, v.sha256, v.body, v.signature)
    with pytest.raises(InvalidSignature):
        verify(signer.public_b64, v.version + 1, v.sha256, v.body, v.signature)
    with pytest.raises(InvalidSignature):
        verify(signer.public_b64, v.version, v.sha256, v.body + " ", v.signature)
    row = conn.execute("SELECT sha256, status FROM config_versions").fetchone()
    assert row == (v.sha256, "pending")


def test_build_without_changes_creates_no_new_version(conn: psycopg.Connection) -> None:
    signer = Signer.generate()
    add_route(conn, 1)
    first = build_version(conn, signer, domain=DOMAIN)
    assert first
    assert build_version(conn, signer, domain=DOMAIN) is None
    add_route(conn, 2)
    second = build_version(conn, signer, domain=DOMAIN)
    assert second and second.version == first.version + 1


# --------------------------------------------------------------- debounce
def test_a_burst_of_route_changes_yields_one_version(dsn: str) -> None:
    signer = Signer.generate()
    pushed: list[int] = []
    settings = Settings(debounce_seconds=0.1)

    def connect() -> psycopg.Connection:
        return psycopg.connect(dsn, autocommit=True)

    ctl = CompilerController(connect, signer, settings, push=lambda v: pushed.append(v.version))
    stop = threading.Event()
    t = threading.Thread(target=ctl.run, args=(stop,))
    t.start()
    try:
        time.sleep(0.3)  # let it become leader and finish its startup compile
        pushed.clear()
        with connect() as c:
            for i in range(30):
                add_route(c, i)
                c.execute("SELECT pg_notify('ipo_routes_changed', %s)", (f"t{i}",))
                time.sleep(0.001)
        deadline = time.time() + 5
        while time.time() < deadline and not pushed:
            time.sleep(0.05)
        time.sleep(0.4)  # anything still queued would arrive now
    finally:
        stop.set()
        t.join(5)
    assert len(pushed) == 1, pushed
    with connect() as c:
        body = c.execute("SELECT body FROM config_versions ORDER BY version DESC").fetchone()
    assert body and len(json.loads(body[0])["http"]["routers"]) == 31


def test_only_the_leader_compiles(dsn: str) -> None:
    signer = Signer.generate()
    seen: list[str] = []

    def connect() -> psycopg.Connection:
        return psycopg.connect(dsn, autocommit=True)

    with connect() as c:
        add_route(c, 1)
    ctls = [CompilerController(connect, signer, Settings(), push=partial(_record, seen, n))
            for n in ("a", "b")]
    stop = threading.Event()
    threads = [threading.Thread(target=c.run, args=(stop,)) for c in ctls]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join(5)
    assert len(seen) == 1  # one version, produced by one leader


# ------------------------------------------------------------------- push
class FakeAgent:
    def __init__(self, name: str) -> None:
        self.name = name
        self.received: list[int] = []
        self.script: list[Callable[[], Ack]] = []

    def put_config(self, envelope: ConfigVersion) -> Ack:
        version = envelope.version
        self.received.append(version)
        if self.script:
            return self.script.pop(0)()
        return Ack(ok=True, live_version=version)


def _reject() -> Ack:
    return Ack(ok=False, live_version=0, error="unknown middleware")


def _record(seen: list[str], name: str, v: ConfigVersion) -> None:
    seen.append(name)


def _boom() -> Ack:
    raise ConnectionError("agent unreachable")


def _pusher(dsn: str, agents: Mapping[str, Agent]) -> Pusher:
    return Pusher(agents, partial(psycopg.connect, dsn, autocommit=True),
                  max_attempts=3, sleep=lambda s: None)


def _version(dsn: str) -> tuple[psycopg.Connection, ConfigVersion]:
    conn = psycopg.connect(dsn, autocommit=True)
    add_route(conn, 1)
    v = build_version(conn, Signer.generate(), domain=DOMAIN)
    assert v
    return conn, v


def test_push_reaches_both_gateways_and_marks_the_version_live(dsn: str) -> None:
    conn, v = _version(dsn)
    agents = {"gw-a": FakeAgent("gw-a"), "gw-b": FakeAgent("gw-b")}
    result = _pusher(dsn, agents).push(v)
    assert result == {"gw-a": "ok", "gw-b": "ok"}
    assert agents["gw-a"].received == agents["gw-b"].received == [1]
    assert conn.execute("SELECT status FROM config_versions").fetchone() == ("live",)
    live: dict[str, int] = dict(
        conn.execute("SELECT gateway, live_version FROM gateway_status").fetchall())
    assert live == {"gw-a": 1, "gw-b": 1}


def test_transport_errors_are_retried_with_backoff(dsn: str) -> None:
    _, v = _version(dsn)
    a = FakeAgent("gw-a")
    a.script = [_boom, _boom]
    result = _pusher(dsn, {"gw-a": a, "gw-b": FakeAgent("gw-b")}).push(v)
    assert result["gw-a"] == "ok" and len(a.received) == 3


def test_an_unreachable_gateway_does_not_block_the_other(dsn: str) -> None:
    conn, v = _version(dsn)
    a = FakeAgent("gw-a")
    a.script = [_boom, _boom, _boom]
    result = _pusher(dsn, {"gw-a": a, "gw-b": FakeAgent("gw-b")}).push(v)
    assert result == {"gw-a": "unreachable", "gw-b": "ok"}
    assert conn.execute("SELECT status FROM config_versions").fetchone() == ("pending",)
    live: dict[str, int] = dict(
        conn.execute("SELECT gateway, live_version FROM gateway_status").fetchall())
    assert live == {"gw-b": 1}


def test_a_rejection_is_not_retried_and_marks_the_version_rejected(dsn: str) -> None:
    conn, v = _version(dsn)
    agents = {}
    for n in ("gw-a", "gw-b"):
        a = FakeAgent(n)
        a.script = [_reject]
        agents[n] = a
    result = _pusher(dsn, agents).push(v)
    assert result == {"gw-a": "rejected", "gw-b": "rejected"}
    assert all(len(a.received) == 1 for a in agents.values())
    assert conn.execute("SELECT status FROM config_versions").fetchone() == ("rejected",)


def test_a_newer_live_version_supersedes_the_older(dsn: str) -> None:
    conn, v1 = _version(dsn)
    agents = {"gw-a": FakeAgent("gw-a"), "gw-b": FakeAgent("gw-b")}
    p = _pusher(dsn, agents)
    p.push(v1)
    add_route(conn, 2)
    v2 = build_version(conn, Signer.generate(), domain=DOMAIN)
    assert v2
    p.push(v2)
    statuses = conn.execute("SELECT version, status FROM config_versions ORDER BY 1").fetchall()
    assert statuses == [(1, "superseded"), (2, "live")]


# --------------------------------------------------------------- rollback
def test_rollback_pins_an_old_body_under_a_new_higher_version(conn: psycopg.Connection) -> None:
    signer = Signer.generate()
    add_route(conn, 1)
    v1 = build_version(conn, signer, domain=DOMAIN)
    add_route(conn, 2)
    v2 = build_version(conn, signer, domain=DOMAIN)
    assert v1 and v2
    rollback(conn, 1)
    v3 = build_version(conn, signer, domain=DOMAIN)
    assert v3 and v3.version == 3 and v3.body == v1.body
    assert build_version(conn, signer, domain=DOMAIN) is None  # pinned: still nothing new
    add_route(conn, 3)  # a new route does not change a pinned config
    assert build_version(conn, signer, domain=DOMAIN) is None
    rollback(conn, None)  # unpin: back to rendering from the routes table
    v4 = build_version(conn, signer, domain=DOMAIN)
    assert v4 and len(json.loads(v4.body)["http"]["routers"]) == 4


def test_rollback_to_an_unknown_version_is_rejected(conn: psycopg.Connection) -> None:
    with pytest.raises(LookupError):
        rollback(conn, 42)


# ------------------------------------------------------------ convergence
def test_wait_for_route_needs_a_version_with_the_route_acked_by_both(dsn: str) -> None:
    conn, v = _version(dsn)
    gw = DbGateways(Settings(), poll_interval=0.02)
    host = f"t1.{DOMAIN}"
    with pytest.raises(TimeoutError):
        gw.wait_for_route(conn, host, timeout=0.2)
    for name in ("gw-a",):
        conn.execute("INSERT INTO gateway_status (gateway, live_version) VALUES (%s, 1)", (name,))
    with pytest.raises(TimeoutError):  # only one gateway has it
        gw.wait_for_route(conn, host, timeout=0.2)
    conn.execute("INSERT INTO gateway_status (gateway, live_version) VALUES ('gw-b', 1)")
    gw.wait_for_route(conn, host, timeout=1)
    with pytest.raises(TimeoutError):  # a route no version contains
        gw.wait_for_route(conn, f"nope.{DOMAIN}", timeout=0.2)
    del v


# -------------------------------------------------------------- HTTP agent
def _serve(status: int, payload: dict[str, object]) -> tuple[object, str, list[dict[str, object]]]:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[dict[str, object]] = []

    class H(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", seen


def test_http_agent_maps_status_codes_to_acks() -> None:
    from ipo.controllers.compiler import ConfigVersion, HttpAgent

    v = ConfigVersion(3, "h", "s", "{}")
    srv, url, seen = _serve(200, {"live_version": 3})
    assert HttpAgent(url).put_config(v) == Ack(True, 3)
    assert seen == [{"version": 3, "sha256": "h", "signature": "s", "body": "{}"}]
    srv.shutdown()  # type: ignore[attr-defined]
    srv, url, _ = _serve(422, {"error": "bad signature", "live_version": 2})
    assert HttpAgent(url).put_config(v) == Ack(False, 2, "bad signature")
    srv.shutdown()  # type: ignore[attr-defined]
    srv, url, _ = _serve(503, {})
    with pytest.raises(ConnectionError):
        HttpAgent(url).put_config(v)
    srv.shutdown()  # type: ignore[attr-defined]
    with pytest.raises(ConnectionError):
        HttpAgent("http://127.0.0.1:1").put_config(v)


def test_wait_for_route_removed_needs_every_gateway_on_a_version_without_it(dsn: str) -> None:
    conn, _ = _version(dsn)  # v1 contains t1
    add_route(conn, 2)
    conn.execute("DELETE FROM routes WHERE host = %s", (f"t1.{DOMAIN}",))
    v2 = build_version(conn, Signer.generate(), domain=DOMAIN)
    assert v2 and f"t1.{DOMAIN}" not in v2.body
    gw = DbGateways(Settings(), poll_interval=0.02)
    host = f"t1.{DOMAIN}"
    conn.execute("INSERT INTO gateway_status (gateway, live_version) VALUES ('gw-a', 2),"
                 " ('gw-b', 1)")
    with pytest.raises(TimeoutError):  # gw-b still serves v1, which has the route
        gw.wait_for_route_removed(conn, host, timeout=0.2)
    conn.execute("UPDATE gateway_status SET live_version = 2 WHERE gateway = 'gw-b'")
    gw.wait_for_route_removed(conn, host, timeout=1)

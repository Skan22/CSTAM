"""The control API: FastAPI app, routes, and the middleware around them."""

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ipo import metrics, runtime_settings
from ipo.api import auth, models, users
from ipo.api.auth import Principal
from ipo.api.problem import Problem, install
from ipo.controllers import compiler
from ipo.domain import events, gateways, leases, traffic
from ipo.domain.registration import (
    IdempotencyConflict,
    PoolExhausted,
    QuotaGuardError,
    SlugTaken,
    register_team,
)
from ipo.domain.teardown import TeamBusy, audit, enqueue_teardown
from ipo.settings import Settings

log = logging.getLogger("ipo.api")

Connect = Callable[[], psycopg.Connection]
PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid token"}, 403: {"description": "Role too low"}}


@dataclass
class AppDeps:
    connect: Connect
    settings: Settings
    jwt_secret: str
    token_ttl_seconds: int = 900
    # Called with the name of the gateway to fault; None where no agent client is wired.
    failover: Callable[[str], None] | None = None
    # Where the Grafana that embeds into the dashboard lives; empty hides the Metrics page.
    grafana_url: str = ""


def grafana_url(raw: str) -> str:
    """A plain http(s) base URL without credentials, query or fragment, or an error. The SPA puts
    this in an iframe src, so anything else (javascript:, userinfo, a query) is refused."""
    raw = raw.strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or "?" in raw or "#" in raw):
        raise ValueError(f"IPO_GRAFANA_URL must be a plain http(s) URL, got {raw!r}")
    return raw.rstrip("/")


class _Middleware:
    """Request ids and latency, as plain ASGI so streaming responses aren't buffered."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-request-id"), "")
        if not (0 < len(rid) <= 128 and rid.isprintable()):
            rid = uuid.uuid4().hex
        scope["ipo.request_id"] = rid
        started = time.perf_counter()

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                route = scope.get("route")
                metrics.API_DURATION.labels(
                    scope["method"], getattr(route, "path", "unmatched"), str(message["status"])
                ).observe(time.perf_counter() - started)
                message["headers"] = [*message["headers"], (b"x-request-id", rid.encode())]
            await send(message)

        await self.app(scope, receive, wrapped)


def _db(request: Request) -> Iterator[psycopg.Connection]:
    conn = request.app.state.deps.connect()
    try:
        yield conn
    finally:
        conn.close()


Db = Depends(_db)


def _rid(request: Request) -> str:
    return str(request.scope.get("ipo.request_id", ""))


def _team_row(r: tuple[Any, ...]) -> models.Team:
    return models.Team(id=r[0], slug=r[1], subdomain=r[2], state=r[3], owner=r[4], ip=r[5],
                       created_at=r[6], expires_at=r[7])


_TEAM_SQL = (
    "SELECT t.id, t.slug, t.subdomain, t.state, t.owner, host(l.ip), t.created_at, t.expires_at"
    " FROM teams t LEFT JOIN leases l ON l.team_id = t.id AND l.state IN ('leased', 'draining')")


def _get_team(conn: psycopg.Connection, team_id: UUID) -> models.Team:
    row = conn.execute(_TEAM_SQL + " WHERE t.id = %s", (team_id,)).fetchone()
    if row is None:
        raise Problem(404, "not-found", "Not Found", f"team {team_id} does not exist")
    return _team_row(row)


def _team_view(conn: psycopg.Connection, team_id: str) -> models.TeamView:
    r = conn.execute("SELECT id, slug, subdomain, state, created_at, expires_at FROM teams"
                     " WHERE id = %s", (team_id,)).fetchone()
    if r is None:
        raise Problem(404, "not-found", "Not Found", "your team no longer exists")
    return models.TeamView(id=r[0], slug=r[1], host=r[2], state=r[3], created_at=r[4],
                           expires_at=r[5])


TEAM_EVENTS = ("team.active", "team.draining", "team.deleted", "team.failed")


def _my_team(deps: AppDeps, team_id: str) -> models.TeamView:
    with deps.connect() as conn:
        return _team_view(conn, team_id)


def team_events(team_id: str, host: str) -> Callable[[str], str | None]:
    """A filter for one team's event stream: its own lifecycle events and a nudge when its host
    served traffic. Everything else, and every other team's data, is dropped."""

    def keep(payload: str) -> str | None:
        try:
            data = json.loads(payload)
        except ValueError:
            return None
        kind = data.get("kind")
        if kind in TEAM_EVENTS and data.get("team_id") == team_id:
            return json.dumps({"kind": kind, "slug": data.get("slug")})
        if kind == "traffic.batch" and host in data.get("hosts", []):
            return json.dumps({"kind": kind})
        return None

    return keep


def _stream(deps: AppDeps, stop: threading.Event,
            select: Callable[[str], str | None] | None = None) -> Iterator[str | None]:
    """Blocking generator over Postgres NOTIFY; owns its connection and closes it on exit.

    Yields None once a second while idle: the server only notices a departed client when it
    is woken to look, since nothing is being written."""
    conn = deps.connect()
    try:
        conn.execute(f"LISTEN {events.CHANNEL}")
        yield ": connected\n\n"
        seq, quiet_since = 0, time.monotonic()
        while not stop.is_set():
            for note in conn.notifies(timeout=1.0):
                payload = select(note.payload) if select else note.payload
                if payload is None:
                    continue
                seq += 1
                try:
                    kind = str(json.loads(payload).get("kind", "message"))
                except (ValueError, AttributeError):
                    kind = "message"
                kind = kind.replace("\n", " ")
                yield f"id: {seq}\nevent: {kind}\ndata: {payload}\n\n"
                quiet_since = time.monotonic()
                if stop.is_set():
                    break
            if time.monotonic() - quiet_since >= 15:
                yield ": keepalive\n\n"
                quiet_since = time.monotonic()
            else:
                yield None
    finally:
        conn.close()


async def _events_body(deps: AppDeps, request: Request,
                       select: Callable[[str], str | None] | None = None) -> AsyncIterator[str]:
    stop = threading.Event()
    it = _stream(deps, stop, select)
    done = object()
    try:
        while True:
            chunk = await run_in_threadpool(next, it, done)
            if chunk is done:
                return
            if chunk is None:
                if await request.is_disconnected():
                    return
                continue
            yield str(chunk)
    except asyncio.CancelledError:
        raise
    finally:
        stop.set()  # the generator exits within a second and closes its own connection


def create_app(deps: AppDeps) -> FastAPI:
    auth.check_secret(deps.jwt_secret)
    app = FastAPI(
        title="Resilient IP Optimizer control API", version="1.0.0",
        description="Registers teams, watches the pool, and controls the gateways.",
        responses=PROBLEM_RESPONSES)
    app.state.deps = deps
    app.add_middleware(_Middleware)
    install(app)

    viewer = Depends(auth.require("viewer"))
    operator = Depends(auth.require("operator"))
    admin = Depends(auth.require("admin"))

    def settings_now(conn: psycopg.Connection) -> Settings:
        return runtime_settings.effective(conn, deps.settings)

    # -------------------------------------------------------------- ops
    @app.get("/healthz", tags=["ops"], responses={})
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"], responses={503: {"description": "Database unreachable"}})
    def readyz() -> JSONResponse:
        try:
            with deps.connect() as conn:
                conn.execute("SELECT 1")
        except Exception:
            return JSONResponse({"status": "not ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics", tags=["ops"], responses={})
    def scrape() -> Response:
        try:
            with deps.connect() as conn:
                metrics.refresh_gauges(
                    conn, pool_target=settings_now(conn).pool_target,
                    gateways=deps.settings.gateways,
                    fresh_seconds=deps.settings.heartbeat_fresh_seconds)
        except Exception:
            log.exception("could not refresh gauges; serving the last values")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------- auth
    @app.post("/v1/auth/login", tags=["auth"], response_model=models.Token, responses={
        401: {"description": "Wrong email or password"}})
    def login(body: models.LoginRequest, conn: psycopg.Connection = Db) -> models.Token:
        who = users.authenticate(conn, body.email, body.password)
        if who is None:
            raise Problem(401, "unauthorized", "Unauthorized", "invalid email or password",
                          headers={"WWW-Authenticate": "Bearer"})
        token = auth.issue(deps.jwt_secret, deps.token_ttl_seconds, body.email, who.role,
                           who.team_id)
        return models.Token(access_token=token, expires_in=deps.token_ttl_seconds, role=who.role)

    @app.get("/v1/me", tags=["auth"], response_model=models.Me)
    def me(who: Principal = Depends(auth.require_any()),
           conn: psycopg.Connection = Db) -> models.Me:
        """The caller's own account; for a team account, its team."""
        return models.Me(email=who.username, role=who.role,
                         team=_team_view(conn, who.team_id) if who.team_id else None)

    # ------------------------------------------------------------ teams
    @app.post("/v1/teams", tags=["teams"], status_code=202, response_model=models.Registered,
              responses={409: {"description": "Slug taken, or key reused for another team"},
                         429: {"description": "Would drop below the free-address reserve"},
                         503: {"description": "No addresses left"}})
    def create_team(
        request: Request, response: Response, body: models.RegisterRequest,
        who: Principal = operator, conn: psycopg.Connection = Db,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> models.Registered:
        if not idempotency_key or len(idempotency_key) > 200:
            raise Problem(400, "idempotency-key", "Bad Request",
                          "send an Idempotency-Key header (1-200 characters) so a retry "
                          "can't register the team twice")
        try:
            # Namespaced so callers can't collide with each other or with internal job keys.
            reg = register_team(conn, settings_now(conn), slug=body.slug, owner=who.username,
                                idempotency_key=f"register:{who.username}:{idempotency_key}",
                                trace_id=_rid(request))
        except ValueError as exc:
            raise Problem(422, "validation", "Invalid request", str(exc)) from exc
        except SlugTaken as exc:
            raise Problem(409, "slug-taken", "Conflict",
                          f"the name {body.slug!r} is already taken") from exc
        except IdempotencyConflict as exc:
            raise Problem(409, "idempotency-conflict", "Conflict",
                          "this Idempotency-Key was already used for a different team") from exc
        except QuotaGuardError as exc:
            raise Problem(429, "quota-guard", "Too Many Requests", str(exc),
                          headers={"Retry-After": "30"}) from exc
        except PoolExhausted as exc:
            raise Problem(503, "pool-exhausted", "Service Unavailable", str(exc),
                          headers={"Retry-After": "30"}) from exc
        if reg.created:
            audit(conn, who.username, "team.register", body.slug, job_id=reg.job_id, ip=reg.ip,
                  path=reg.path)
        else:
            response.headers["Idempotent-Replayed"] = "true"
        return models.Registered(
            job_id=UUID(reg.job_id), team_id=UUID(reg.team_id), ip=reg.ip, path=reg.path,
            subdomain=reg.subdomain, status_url=f"/v1/jobs/{reg.job_id}")

    @app.get("/v1/teams", tags=["teams"], response_model=list[models.Team])
    def list_teams(
        state: str | None = Query(None, pattern="^(pending|active|draining|deleted|failed)$"),
        include_deleted: bool = False, _: Principal = viewer, conn: psycopg.Connection = Db,
    ) -> list[models.Team]:
        where, args = [], []
        if state:
            where.append("t.state = %s")
            args.append(state)
        elif not include_deleted:
            where.append("t.state <> 'deleted'")
        sql = _TEAM_SQL + (" WHERE " + " AND ".join(where) if where else "")
        rows = conn.execute(sql + " ORDER BY t.created_at, t.slug", args).fetchall()
        return [_team_row(r) for r in rows]

    @app.get("/v1/teams/{team_id}", tags=["teams"], response_model=models.Team,
             responses={404: {"description": "No such team"}})
    def get_team(team_id: UUID, _: Principal = viewer,
                 conn: psycopg.Connection = Db) -> models.Team:
        return _get_team(conn, team_id)

    @app.delete("/v1/teams/{team_id}", tags=["teams"], status_code=202,
                response_model=models.Teardown, responses={
                    200: {"description": "Already deleted", "model": models.Team},
                    404: {"description": "No such team"},
                    409: {"description": "Still being registered"}})
    def delete_team(team_id: UUID, who: Principal = operator,
                    conn: psycopg.Connection = Db) -> Response | models.Teardown:
        try:
            job = enqueue_teardown(conn, str(team_id), reason="manual", actor=who.username)
        except LookupError as exc:
            raise Problem(404, "not-found", "Not Found", f"team {team_id} does not exist") from exc
        except TeamBusy as exc:
            raise Problem(409, "team-busy", "Conflict", str(exc),
                          headers={"Retry-After": "5"}) from exc
        if job is None:
            return JSONResponse(_get_team(conn, team_id).model_dump(mode="json"))
        return models.Teardown(job_id=UUID(job), team_id=team_id, status_url=f"/v1/jobs/{job}")

    @app.post("/v1/teams/{team_id}/extend", tags=["teams"], response_model=models.Team,
              responses={404: {"description": "No such team"},
                         409: {"description": "Team is not active"}})
    def extend_team(team_id: UUID, body: models.ExtendRequest, who: Principal = operator,
                    conn: psycopg.Connection = Db) -> models.Team:
        with conn.transaction():
            row = conn.execute(
                "UPDATE teams SET expires_at = expires_at + make_interval(secs => %s)"
                " WHERE id = %s AND state = 'active' RETURNING expires_at, slug",
                (body.seconds, team_id)).fetchone()
            if row is None:
                _get_team(conn, team_id)  # 404 if unknown
                raise Problem(409, "team-not-active", "Conflict",
                              "only an active team's lease can be extended")
            conn.execute("UPDATE leases SET expires_at = %s WHERE team_id = %s"
                         " AND state = 'leased'", (row[0], team_id))
            audit(conn, who.username, "team.extend", row[1], seconds=body.seconds)
        return _get_team(conn, team_id)

    # ------------------------------------------------------------- jobs
    @app.get("/v1/jobs/{job_id}", tags=["jobs"], response_model=models.Job,
             responses={404: {"description": "No such job"}})
    def get_job(job_id: UUID, _: Principal = viewer, conn: psycopg.Connection = Db) -> models.Job:
        row = conn.execute(
            "SELECT id, kind, state, team_id, attempts, last_error, trace_id, created_at"
            " FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise Problem(404, "not-found", "Not Found", f"job {job_id} does not exist")
        steps = [models.Step(step=s, status=st, started_at=a, finished_at=b, result=r)
                 for s, st, a, b, r in conn.execute(
                     "SELECT step, status, started_at, finished_at, result FROM saga_steps"
                     " WHERE job_id = %s ORDER BY started_at, step", (job_id,))]
        return models.Job(id=row[0], kind=row[1], state=row[2], team_id=row[3], attempts=row[4],
                          last_error=row[5], trace_id=row[6], created_at=row[7], steps=steps)

    # ------------------------------------------------------- pool, ipam
    @app.get("/v1/ui", tags=["pool"], response_model=models.UiConfig)
    def ui_config(_: Principal = viewer) -> models.UiConfig:
        return models.UiConfig(grafana_url=grafana_url(deps.grafana_url))

    @app.get("/v1/pool", tags=["pool"], response_model=models.Pool)
    def pool(_: Principal = viewer, conn: psycopg.Connection = Db) -> models.Pool:
        c = leases.counts(conn)
        return models.Pool(
            size=c.get("pooled", 0), target=settings_now(conn).pool_target,
            free=c.get("free", 0), leased=c.get("leased", 0), draining=c.get("draining", 0),
            quarantined=c.get("quarantined", 0))

    @app.get("/v1/ipam", tags=["pool"], response_model=models.Ipam)
    def ipam(_: Principal = viewer, conn: psycopg.Connection = Db) -> models.Ipam:
        rows = conn.execute(
            "SELECT host(l.ip), l.state::text, t.slug, l.expires_at, l.quarantined_until"
            " FROM leases l LEFT JOIN teams t ON t.id = l.team_id ORDER BY l.ip").fetchall()
        items = [models.IpamLease(ip=r[0], state=r[1], team=r[2], expires_at=r[3],
                                  quarantined_until=r[4]) for r in rows]
        return models.Ipam(total=len(items), counts=leases.counts(conn), leases=items)

    # --------------------------------------------------------- gateways
    @app.get("/v1/gateways", tags=["gateways"], response_model=models.Gateways)
    def list_gateways(_: Principal = viewer, conn: psycopg.Connection = Db) -> models.Gateways:
        known = {r[0]: r for r in conn.execute(
            "SELECT gateway, vrrp_state, live_version, last_heartbeat FROM gateway_status")}
        out = [models.Gateway(gateway=g, vrrp_state=known[g][1], live_version=known[g][2],
                              last_heartbeat=known[g][3]) if g in known else
               models.Gateway(gateway=g, vrrp_state="UNKNOWN", live_version=None,
                              last_heartbeat=None) for g in deps.settings.gateways]
        masters = gateways.masters(conn, deps.settings.gateways,
                                   deps.settings.heartbeat_fresh_seconds)
        return models.Gateways(gateways=out, split_brain=len(masters) > 1)

    @app.post("/v1/gateways/failover", tags=["gateways"], status_code=202,
              response_model=models.Failover, responses={
                  409: {"description": "No gateway is MASTER"},
                  501: {"description": "No agent client configured"}})
    def failover(body: models.FailoverRequest, who: Principal = admin,
                 conn: psycopg.Connection = Db) -> models.Failover:
        if deps.failover is None:
            raise Problem(501, "not-implemented", "Not Implemented",
                          "this deployment has no gateway agent client")
        row = conn.execute(
            "SELECT gateway FROM gateway_status WHERE vrrp_state = 'MASTER'"
            " AND gateway = ANY(%s) ORDER BY gateway LIMIT 1",
            (list(deps.settings.gateways),)).fetchone()
        if row is None:
            raise Problem(409, "no-master", "Conflict", "no gateway currently reports MASTER")
        try:
            deps.failover(row[0])
        except (ConnectionError, OSError) as exc:
            raise Problem(502, "agent-unreachable", "Bad Gateway",
                          f"gateway {row[0]} did not accept the fault: {exc}") from exc
        audit(conn, who.username, "gateway.failover", row[0])
        return models.Failover(gateway=row[0])

    @app.post("/v1/gateways/{name}/heartbeat", tags=["gateways"], status_code=204,
              responses={404: {"description": "Not a configured gateway"}})
    def heartbeat(name: str, body: models.Heartbeat, _: Principal = operator,
                  conn: psycopg.Connection = Db) -> Response:
        """Sent by each gateway agent every few seconds with its VRRP state and live version."""
        if name not in deps.settings.gateways:
            raise Problem(404, "not-found", "Not Found", f"gateway {name} is not configured")
        with conn.transaction():
            prev = conn.execute("SELECT vrrp_state FROM gateway_status WHERE gateway = %s"
                                " FOR UPDATE", (name,)).fetchone()
            was = gateways.masters(conn, deps.settings.gateways,
                                   deps.settings.heartbeat_fresh_seconds)
            conn.execute(
                "INSERT INTO gateway_status (gateway, vrrp_state, live_version, last_heartbeat)"
                " VALUES (%s, %s, %s, now()) ON CONFLICT (gateway) DO UPDATE SET"
                " vrrp_state = EXCLUDED.vrrp_state,"
                " live_version = COALESCE(EXCLUDED.live_version, gateway_status.live_version),"
                " last_heartbeat = now()", (name, body.vrrp_state, body.live_version or None))
            if (prev[0] if prev else None) != body.vrrp_state:
                events.emit(conn, "gateway.vrrp", gateway=name, to=body.vrrp_state,
                            **{"from": prev[0] if prev else None})
            now = gateways.masters(conn, deps.settings.gateways,
                                   deps.settings.heartbeat_fresh_seconds)
            if len(now) > 1 >= len(was):
                metrics.SPLIT_BRAINS.inc()
                events.emit(conn, "gateway.split_brain", masters=now)
            elif len(now) <= 1 < len(was):
                events.emit(conn, "gateway.split_brain_resolved", masters=now)
        return Response(status_code=204)

    # ---------------------------------------------------------- traffic
    @app.post("/v1/gateways/{name}/traffic", tags=["traffic"], status_code=204,
              response_class=Response, responses={404: {"description": "Unknown gateway"}})
    def report_traffic(name: str, body: models.TrafficReport, _: Principal = operator,
                       conn: psycopg.Connection = Db) -> Response:
        """Sent by each gateway agent every few seconds with what Traefik served."""
        if name not in deps.settings.gateways:
            raise Problem(404, "not-found", "Not Found", f"gateway {name} is not configured")
        traffic.ingest(conn, name, [h.model_dump() for h in body.hosts],
                       [r.model_dump(mode="json") for r in body.recent])
        return Response(status_code=204)

    @app.get("/v1/traffic", tags=["traffic"], response_model=models.Traffic)
    def get_traffic(window_minutes: int = Query(60, ge=1, le=1440), _: Principal = viewer,
                    conn: psycopg.Connection = Db) -> dict[str, Any]:
        """Requests, errors, bytes and latency per team over the window, busiest first."""
        return traffic.summary(conn, window_minutes)

    @app.get("/v1/traffic/recent", tags=["traffic"], response_model=models.RecentTrafficList)
    def get_recent_traffic(host: str | None = Query(None, max_length=253),
                           limit: int = Query(50, ge=1, le=200), _: Principal = viewer,
                           conn: psycopg.Connection = Db) -> dict[str, Any]:
        return {"requests": traffic.recent(conn, limit, host=host.lower() if host else None)}

    # ------------------------------------------------------- team accounts
    @app.post("/v1/teams/{team_id}/users", tags=["teams"], status_code=201,
              response_model=models.TeamUser, responses={
                  404: {"description": "No such team"},
                  409: {"description": "That email already has an account"}})
    def create_team_user(team_id: UUID, body: models.TeamUserRequest, who: Principal = admin,
                         conn: psycopg.Connection = Db) -> models.TeamUser:
        """Give a team a login that sees only that team's own traffic."""
        row = conn.execute("SELECT slug FROM teams WHERE id = %s AND state <> 'deleted'",
                           (team_id,)).fetchone()
        if row is None:
            raise Problem(404, "not-found", "Not Found", f"team {team_id} does not exist")
        try:
            users.create_team_user(conn, str(team_id), body.email, body.password)
        except users.EmailTaken as exc:
            raise Problem(409, "email-taken", "Conflict",
                          "that email already has an account") from exc
        audit(conn, who.username, "user.create", body.email, team=row[0], role="team")
        return models.TeamUser(email=body.email, team_id=team_id)

    team_only = Depends(auth.require_team())

    @app.get("/v1/me/traffic", tags=["me"], response_model=models.Traffic)
    def my_traffic(window_minutes: int = Query(60, ge=1, le=1440), who: Principal = team_only,
                   conn: psycopg.Connection = Db) -> dict[str, Any]:
        """Your team's requests, errors, bytes and latency over the window."""
        return traffic.summary(conn, window_minutes, team_id=who.team_id)

    @app.get("/v1/me/traffic/recent", tags=["me"], response_model=models.RecentTrafficList)
    def my_recent_traffic(limit: int = Query(50, ge=1, le=200), who: Principal = team_only,
                          conn: psycopg.Connection = Db) -> dict[str, Any]:
        return {"requests": traffic.recent(conn, limit, team_id=who.team_id)}

    @app.get("/v1/me/events", tags=["me"], response_class=StreamingResponse, responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}})
    async def my_events(
        request: Request, who: Principal = Depends(auth.require_team(query_token=True)),
    ) -> StreamingResponse:
        """Only your team's lifecycle events, and a nudge when your host served traffic."""
        assert who.team_id
        view = await run_in_threadpool(_my_team, deps, who.team_id)
        return StreamingResponse(
            _events_body(deps, request, team_events(who.team_id, view.host)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ----------------------------------------------------------- config
    @app.get("/v1/config/latest", tags=["config"], response_model=models.ConfigEnvelope,
             responses={404: {"description": "No config has been compiled yet"}})
    def config_latest(_: Principal = operator,
                      conn: psycopg.Connection = Db) -> models.ConfigEnvelope:
        """What a gateway agent's pull loop fetches in case a push was missed."""
        r = conn.execute("SELECT version, sha256, signature, body FROM config_versions"
                         " WHERE status IN ('pending', 'live') ORDER BY version DESC LIMIT 1"
                         ).fetchone()
        if r is None:
            raise Problem(404, "not-found", "Not Found", "no config version has been compiled")
        return models.ConfigEnvelope(version=r[0], sha256=r[1], signature=r[2], body=r[3])

    @app.get("/v1/config/versions", tags=["config"], response_model=models.ConfigVersions)
    def config_versions(limit: int = Query(50, ge=1, le=500), _: Principal = admin,
                        conn: psycopg.Connection = Db) -> models.ConfigVersions:
        rows = conn.execute(
            "SELECT version, sha256, status, created_at FROM config_versions"
            " ORDER BY version DESC LIMIT %s", (limit,)).fetchall()
        return models.ConfigVersions(
            versions=[models.ConfigVersionInfo(version=r[0], sha256=r[1], status=r[2],
                                               created_at=r[3]) for r in rows],
            pinned=compiler._pin(conn))

    @app.get("/v1/config/versions/{version}", tags=["config"],
             response_model=models.ConfigVersionDetail,
             responses={404: {"description": "No such version"}})
    def config_version(version: int, _: Principal = admin,
                       conn: psycopg.Connection = Db) -> models.ConfigVersionDetail:
        r = conn.execute("SELECT version, sha256, status, created_at, signature, body"
                         " FROM config_versions WHERE version = %s", (version,)).fetchone()
        if r is None:
            raise Problem(404, "not-found", "Not Found", f"config version {version} not found")
        return models.ConfigVersionDetail(version=r[0], sha256=r[1], status=r[2], created_at=r[3],
                                          signature=r[4], body=r[5])

    @app.post("/v1/config/rollback", tags=["config"], status_code=202,
              response_model=models.Rollback,
              responses={404: {"description": "No such version"}})
    def config_rollback(body: models.RollbackRequest, who: Principal = admin,
                        conn: psycopg.Connection = Db) -> models.Rollback:
        try:
            compiler.rollback(conn, body.version)
        except LookupError as exc:
            raise Problem(404, "not-found", "Not Found", str(exc)) from exc
        audit(conn, who.username, "config.rollback", str(body.version))
        return models.Rollback(pinned=body.version)

    # --------------------------------------------------------- settings
    def settings_view(conn: psycopg.Connection) -> models.SettingsView:
        history = [models.SettingsChange(at=at, actor=actor, detail=detail)
                   for at, actor, detail in conn.execute(
                       "SELECT at, actor, detail FROM audit_log WHERE action = 'settings.update'"
                       " ORDER BY id DESC LIMIT 20")]
        return models.SettingsView(values=runtime_settings.values(settings_now(conn)),
                                   history=history)

    @app.get("/v1/settings", tags=["settings"], response_model=models.SettingsView)
    def get_settings(_: Principal = admin, conn: psycopg.Connection = Db) -> models.SettingsView:
        return settings_view(conn)

    @app.patch("/v1/settings", tags=["settings"], response_model=models.SettingsView)
    def patch_settings(body: runtime_settings.SettingsPatch, who: Principal = admin,
                       conn: psycopg.Connection = Db) -> models.SettingsView:
        runtime_settings.update(conn, body, actor=who.username)
        return settings_view(conn)

    # ------------------------------------------------------ audit, events
    @app.get("/v1/audit", tags=["audit"], response_model=models.Audit)
    def get_audit(limit: int = Query(100, ge=1, le=1000), before_id: int | None = None,
                  _: Principal = admin, conn: psycopg.Connection = Db) -> models.Audit:
        bad = conn.execute("SELECT verify_audit_chain()").fetchone()
        rows = conn.execute(
            "SELECT id, at, actor, action, target, detail FROM audit_log"
            " WHERE (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
            (before_id, before_id, limit)).fetchall()
        first_bad = bad[0] if bad else None
        return models.Audit(
            chain=models.ChainStatus(valid=first_bad is None, first_bad_id=first_bad),
            entries=[models.AuditEntry(id=r[0], at=r[1], actor=r[2], action=r[3], target=r[4],
                                       detail=r[5]) for r in rows])

    @app.get("/v1/events", tags=["events"], response_class=StreamingResponse, responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"}})
    async def stream_events(
        request: Request, _: Principal = Depends(auth.require("viewer", query_token=True)),
    ) -> StreamingResponse:
        return StreamingResponse(
            _events_body(deps, request), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app

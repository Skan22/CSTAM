"""Short-lived HS256 tokens and the role check every route depends on."""

import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ipo.api.problem import Problem
from ipo.api.users import ALL_ROLES, GATEWAY, ROLES, TEAM

ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32
_bearer = HTTPBearer(auto_error=False, description="Token from POST /v1/auth/login")


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    team_id: str | None = None
    gateway: str | None = None


def check_secret(secret: str) -> None:
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise ValueError(f"the JWT secret must be at least {MIN_SECRET_BYTES} bytes")


def issue(secret: str, ttl: int, username: str, role: str, team_id: str | None = None,
          gateway: str | None = None) -> str:
    now = int(time.time())
    claims: dict[str, object] = {"sub": username, "role": role, "iat": now, "exp": now + ttl}
    if team_id:
        claims["team"] = team_id
    if gateway:
        claims["gateway"] = gateway
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def _unauthorized(detail: str) -> Problem:
    return Problem(401, "unauthorized", "Unauthorized", detail,
                   headers={"WWW-Authenticate": "Bearer"})


def decode(secret: str, token: str) -> Principal:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM],
                            options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError as exc:
        raise _unauthorized("invalid or expired token") from exc
    team = claims.get("team")
    if claims["role"] not in ALL_ROLES or not isinstance(claims["sub"], str):
        raise _unauthorized("invalid or expired token")
    if (claims["role"] == TEAM) != isinstance(team, str):
        raise _unauthorized("invalid or expired token")
    gateway = claims.get("gateway")
    if (claims["role"] == GATEWAY) != isinstance(gateway, str):
        raise _unauthorized("invalid or expired token")
    return Principal(claims["sub"], claims["role"], team, gateway)


def require(minimum: str, *, query_token: bool = False) -> Callable[..., Principal]:
    """A dependency that admits callers whose role is at least `minimum`.

    `query_token` lets the browser's EventSource, which can't set headers, pass the token as
    `?access_token=`. It is offered only where a stream needs it, since URLs end up in logs.
    """
    need = ROLES.index(minimum)

    def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        who = _token(request, credentials, query_token)
        if who.role == TEAM:
            raise Problem(403, "forbidden", "Forbidden",
                          "team accounts can only use the /v1/me routes")
        if who.role == GATEWAY:
            raise Problem(403, "forbidden", "Forbidden",
                          "gateway accounts can only heartbeat, report traffic and pull the config")
        if ROLES.index(who.role) < need:
            raise Problem(403, "forbidden", "Forbidden",
                          f"this needs the {minimum} role; you are {who.role}")
        return who

    return dependency


def _token(request: Request, credentials: HTTPAuthorizationCredentials | None,
           query_token: bool) -> Principal:
    token = credentials.credentials if credentials else None
    if token is None and query_token:
        token = request.query_params.get("access_token")
    if not token:
        raise _unauthorized("missing bearer token")
    deps = request.app.state.deps
    who = decode(deps.jwt_secret, token)
    if who.role == GATEWAY:
        with deps.connect() as conn:  # a removed or re-purposed agent account stops working at once
            alive = conn.execute("SELECT 1 FROM users WHERE email = %s AND role = 'gateway'"
                                 " AND gateway = %s", (who.username, who.gateway)).fetchone()
        if not alive:
            raise _unauthorized("this account no longer exists")
    if who.role == TEAM:
        # A token outlives its account: refuse it once the team is gone or its login removed.
        with deps.connect() as conn:
            alive = conn.execute(
                "SELECT 1 FROM users u JOIN teams t ON t.id = u.team_id"
                " WHERE u.email = %s AND u.team_id = %s AND t.state <> 'deleted'",
                (who.username, who.team_id)).fetchone()
        if not alive:
            raise _unauthorized("this account no longer exists")
    request.state.principal = who
    return who


def require_agent() -> Callable[..., Principal]:
    """The routes a gateway agent uses: an operator or better (people debugging a gateway), or the
    agent account of exactly the gateway named in the path (`/v1/gateways/{name}/...`; for routes
    without a name, any gateway agent)."""
    need = ROLES.index("operator")

    def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> Principal:
        who = _token(request, credentials, False)
        if who.role == GATEWAY:
            named = request.path_params.get("name")
            if named is not None and named != who.gateway:
                raise Problem(403, "forbidden", "Forbidden",
                              f"this account belongs to gateway {who.gateway}, not {named}")
            return who
        if who.role == TEAM or ROLES.index(who.role) < need:
            raise Problem(403, "forbidden", "Forbidden",
                          "this is for gateway agents and operators")
        return who

    return dependency


def require_any(*, query_token: bool = False) -> Callable[..., Principal]:
    """Any signed-in account, staff or team: for routes that only describe the caller."""

    def dependency(request: Request,
                   credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
                   ) -> Principal:
        who = _token(request, credentials, query_token)
        if who.role == GATEWAY:
            raise Problem(403, "forbidden", "Forbidden",
                          "gateway accounts can only heartbeat, report traffic and pull the config")
        return who

    return dependency


def require_team(*, query_token: bool = False) -> Callable[..., Principal]:
    """A team account, and nobody else: the data it is handed is scoped to `team_id`."""

    def dependency(request: Request,
                   credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)
                   ) -> Principal:
        who = _token(request, credentials, query_token)
        if who.role != TEAM:
            raise Problem(403, "forbidden", "Forbidden", "this is for team accounts")
        return who

    return dependency

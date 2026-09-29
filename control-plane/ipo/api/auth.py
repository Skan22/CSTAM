"""Short-lived HS256 tokens and the role check every route depends on."""

import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ipo.api.problem import Problem
from ipo.api.users import ROLES

ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32
_bearer = HTTPBearer(auto_error=False, description="Token from POST /v1/auth/login")


@dataclass(frozen=True)
class Principal:
    username: str
    role: str


def check_secret(secret: str) -> None:
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise ValueError(f"the JWT secret must be at least {MIN_SECRET_BYTES} bytes")


def issue(secret: str, ttl: int, username: str, role: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + ttl}, secret,
                      algorithm=ALGORITHM)


def _unauthorized(detail: str) -> Problem:
    return Problem(401, "unauthorized", "Unauthorized", detail,
                   headers={"WWW-Authenticate": "Bearer"})


def decode(secret: str, token: str) -> Principal:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM],
                            options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError as exc:
        raise _unauthorized("invalid or expired token") from exc
    if claims["role"] not in ROLES or not isinstance(claims["sub"], str):
        raise _unauthorized("invalid or expired token")
    return Principal(claims["sub"], claims["role"])


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
        token = credentials.credentials if credentials else None
        if token is None and query_token:
            token = request.query_params.get("access_token")
        if not token:
            raise _unauthorized("missing bearer token")
        who = decode(request.app.state.deps.jwt_secret, token)
        if ROLES.index(who.role) < need:
            raise Problem(403, "forbidden", "Forbidden",
                          f"this needs the {minimum} role; you are {who.role}")
        request.state.principal = who
        return who

    return dependency

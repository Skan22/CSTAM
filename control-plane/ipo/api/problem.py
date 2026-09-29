"""RFC 9457 problem+json errors, so every failure has the same shape."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

MEDIA_TYPE = "application/problem+json"
BASE = "https://ipo.felcloud.tn/problems/"


class Problem(Exception):
    def __init__(self, status: int, slug: str, title: str, detail: str,
                 headers: dict[str, str] | None = None, **extra: Any) -> None:
        super().__init__(detail)
        self.status, self.slug, self.title, self.detail = status, slug, title, detail
        self.headers, self.extra = headers or {}, extra


def _response(request: Request, status: int, slug: str, title: str, detail: str,
              headers: dict[str, str] | None = None, **extra: Any) -> JSONResponse:
    body = {"type": BASE + slug, "title": title, "status": status, "detail": detail,
            "instance": request.url.path, **extra}
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


_TITLES = {401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed"}


def install(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        return _response(request, exc.status, exc.slug, exc.title, exc.detail, exc.headers,
                         **exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        title = _TITLES.get(exc.status_code, "Error")
        headers = dict(exc.headers) if exc.headers else None
        return _response(request, exc.status_code, title.lower().replace(" ", "-"), title,
                         str(exc.detail), headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in exc.errors()]
        return _response(request, 422, "validation", "Invalid request",
                         "; ".join(f"{'.'.join(e['loc'])}: {e['msg']}" for e in errors),
                         errors=errors)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        import logging

        import psycopg

        logging.getLogger("ipo.api").exception("unhandled error on %s", request.url.path)
        if isinstance(exc, psycopg.OperationalError):
            return _response(request, 503, "database-unavailable", "Service Unavailable",
                             "the database is unavailable", {"Retry-After": "5"})
        return _response(request, 500, "internal", "Internal Server Error",
                         "something went wrong; the incident has been logged")

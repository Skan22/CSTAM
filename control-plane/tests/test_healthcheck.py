"""The container's health check: `python -m ipo.healthcheck`, a command with nothing to quote."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ipo import healthcheck


@pytest.fixture
def server() -> Iterator[tuple[HTTPServer, list[int]]]:
    status = [200]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status[0] if self.path == "/readyz" else 404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv, status
    finally:
        srv.shutdown()


def test_healthy_when_readyz_answers_200(server: tuple[HTTPServer, list[int]]) -> None:
    srv, _ = server
    assert healthcheck.check({"IPO_BIND": f"0.0.0.0:{srv.server_port}"}) == 0


def test_unhealthy_when_readyz_fails_or_nothing_listens(
        server: tuple[HTTPServer, list[int]]) -> None:
    srv, status = server
    status[0] = 503
    assert healthcheck.check({"IPO_BIND": f"0.0.0.0:{srv.server_port}"}) == 1
    assert healthcheck.check({"IPO_BIND": "127.0.0.1:9"}) == 1

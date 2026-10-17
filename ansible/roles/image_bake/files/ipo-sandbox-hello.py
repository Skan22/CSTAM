#!/usr/bin/env python3
"""The page every sandbox serves from its first boot, until the team puts its own app on port 80.

It is a plain HTTP responder, so the platform's route probe has something to find the moment a
team is registered, and the team can see which sandbox they got. Stop and disable
`ipo-sandbox-hello.service` to free the port.
"""

import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        host = self.headers.get("Host", "")
        body = (f"sandbox ok host={host} node={socket.gethostname()} "
                f"addr={self.connection.getsockname()[0]}\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET  # noqa: N815

    def log_message(self, *args: object) -> None:
        return None


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104

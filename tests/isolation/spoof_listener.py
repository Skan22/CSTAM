"""Records every datagram that reaches UDP 5140, with the source address it claims, and serves
the list as JSON on TCP 8099 (the real-cloud port-security check's victim). Standard library only."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

seen: list[dict[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(seen).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        return None


def serve() -> None:
    HTTPServer(("0.0.0.0", 8099), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    threading.Thread(target=serve, daemon=True).start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 5140))  # noqa: S104
    while True:
        data, (src, _) = sock.recvfrom(2048)
        seen.append({"src": src, "payload": data.decode(errors="replace")})

"""Dials a batch of addresses at once and reports what came back. Runs inside a lab namespace (via
nsenter, reading stdin) or on a real machine (imported by isolation.real).

stdin: {"tag": str, "probes": [{"addr", "proto", "port"}]}; stdout: one verdict per probe, in
order: `open` (connected, or a UDP reply), `refused` (a reset or ICMP port unreachable: the packet
reached a host) or `blocked` (nothing came back, or the network said unreachable). Each UDP
datagram carries `<tag>:<index>` so a listener can report deliveries that got no reply.
"""

import errno
import json
import selectors
import socket
import sys
import time
from typing import Any

TIMEOUT = 1.5
RESEND_AT = 0.5  # UDP has no handshake, so one lost datagram must not look like a block
BATCH = 400


def probe(probes: list[dict[str, Any]], tag: str = "probe", timeout: float = TIMEOUT) -> list[str]:
    """Verdicts in order. Runs in batches so a sweep of thousands of ports stays under the open
    file limit; the datagram tags keep counting across batches."""
    out: list[str] = []
    for start in range(0, len(probes), BATCH):
        out += _batch(probes[start:start + BATCH], tag, start, timeout)
    return out


def _batch(probes: list[dict[str, Any]], tag: str, offset: int, timeout: float) -> list[str]:
    verdicts = ["blocked"] * len(probes)
    sel = selectors.DefaultSelector()
    live: dict[int, socket.socket] = {}

    def send(i: int, s: socket.socket) -> None:
        try:
            s.send(f"{tag}:{offset + i}".encode())
        except ConnectionRefusedError:
            verdicts[i] = "refused"
        except OSError:
            pass

    for i, p in enumerate(probes):
        tcp = p["proto"] == "tcp"
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM)
        s.setblocking(False)
        try:
            if tcp:
                if s.connect_ex((p["addr"], p["port"])) not in (0, errno.EINPROGRESS):
                    s.close()
                    continue
            else:
                s.connect((p["addr"], p["port"]))
                send(i, s)
        except OSError:
            s.close()
            continue
        sel.register(s, selectors.EVENT_WRITE if tcp else selectors.EVENT_READ, i)
        live[i] = s
    start = time.monotonic()
    resent = False
    while live and time.monotonic() - start < timeout:
        if not resent and time.monotonic() - start > RESEND_AT:
            resent = True
            for i, s in live.items():
                if probes[i]["proto"] == "udp":
                    send(i, s)
        for key, _ in sel.select(0.05):
            i, s = key.data, live[key.data]
            if probes[i]["proto"] == "tcp":
                err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                verdicts[i] = "open" if err == 0 else (
                    "refused" if err == errno.ECONNREFUSED else "blocked")
            else:
                try:
                    s.recv(64)
                    verdicts[i] = "open"
                except ConnectionRefusedError:
                    verdicts[i] = "refused"
                except OSError:
                    verdicts[i] = "blocked"
            sel.unregister(s)
            s.close()
            del live[i]
    for s in live.values():
        s.close()
    return verdicts


if __name__ == "__main__":
    req = json.loads(sys.stdin.read())
    json.dump(probe(req["probes"], req["tag"]), sys.stdout)

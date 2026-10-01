"""Listens on every probe port inside a lab host, so a probe's verdict depends only on the network,
and logs what arrived, so one-way deliveries (UDP with no reply, raw VRRP, forged frames) are seen.

usage: listener.py LOGFILE TCP_PORTS UDP_PORTS [RAW_PROTOS]   (comma-separated)
Log lines: `<proto> <port> <destination address> <source address> <payload>`.
"""

import contextlib
import selectors
import socket
import struct
import sys
from typing import TextIO


def ports(arg: str) -> list[int]:
    return [int(x) for x in arg.split(",") if x]


def log_udp(s: socket.socket, log: TextIO) -> None:
    data, anc, _, peer = s.recvmsg(2048, socket.CMSG_SPACE(12))
    dst = next(socket.inet_ntoa(value[8:12])  # in_pktinfo.ipi_addr: the header's destination
               for level, kind, value in anc
               if level == socket.IPPROTO_IP and kind == socket.IP_PKTINFO)
    log.write(f"udp {s.getsockname()[1]} {dst} {peer[0]} {data.decode(errors='replace')}\n")
    # Answer from the address that was dialled, as real UDP services do: on a host with several
    # addresses the routing table would otherwise pick another one and the client would drop it.
    pktinfo = struct.pack("=I4s4s", 0, socket.inet_aton(dst), bytes(4))
    with contextlib.suppress(OSError):
        s.sendmsg([b"ok"], [(socket.IPPROTO_IP, socket.IP_PKTINFO, pktinfo)], 0, peer)


def main(log_path: str, tcp: str, udp: str, raw: str = "") -> None:
    sel = selectors.DefaultSelector()
    for port in ports(tcp):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(512)
        sel.register(s, selectors.EVENT_READ, "tcp")
    for port in ports(udp):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
        s.bind(("0.0.0.0", port))
        sel.register(s, selectors.EVENT_READ, "udp")
    for proto in ports(raw):
        sel.register(socket.socket(socket.AF_INET, socket.SOCK_RAW, proto), selectors.EVENT_READ,
                     "raw")
    with open(log_path, "a", buffering=1) as log:
        print("ready", flush=True)
        while True:
            for key, _ in sel.select():
                ready = key.fileobj
                assert isinstance(ready, socket.socket)
                try:
                    if key.data == "tcp":
                        c, peer = ready.accept()
                        log.write(f"tcp {ready.getsockname()[1]} {c.getsockname()[0]} {peer[0]} -\n")
                        c.close()
                    elif key.data == "udp":
                        log_udp(ready, log)
                    else:
                        pkt = ready.recv(2048)
                        hl = (pkt[0] & 0x0F) * 4
                        log.write(f"raw{pkt[9]} 0 {socket.inet_ntoa(pkt[16:20])} "
                                  f"{socket.inet_ntoa(pkt[12:16])} "
                                  f"{pkt[hl:].decode(errors='replace')}\n")
                except OSError:
                    continue


if __name__ == "__main__":
    main(*sys.argv[1:])

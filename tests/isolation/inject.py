"""Sends packets a machine has no business sending, from inside a lab namespace.

usage: inject.py frame IFACE DST_MAC SRC_MAC SRC_IP DST_IP DPORT PAYLOAD
           a UDP datagram in a hand-built Ethernet frame: both source addresses are forged at will
       inject.py ipproto PROTO DST_IP PAYLOAD
           a raw IP packet of protocol PROTO (112 is VRRP), routed normally
       inject.py flood DST_IP PORT COUNT RATE PAYLOAD
           COUNT UDP datagrams at RATE per second, each tagged PAYLOAD:<n>
"""

import socket
import struct
import sys
import time


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(int(x) for x in struct.unpack(f"!{len(data) // 2}H", data))
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def udp_packet(src: str, dst: str, dport: int, payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", 40000, dport, 8 + len(payload), 0) + payload  # checksum 0: none
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0x4242, 0, 64, 17, 0,
                      socket.inet_aton(src), socket.inet_aton(dst))
    return hdr[:10] + struct.pack("!H", checksum(hdr)) + hdr[12:] + udp


def mac(text: str) -> bytes:
    return bytes(int(x, 16) for x in text.split(":"))


def main(kind: str, *args: str) -> None:
    if kind == "frame":
        iface, dst_mac, src_mac, src_ip, dst_ip, dport, payload = args
        with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as s:
            s.bind((iface, 0))
            s.send(mac(dst_mac) + mac(src_mac) + b"\x08\x00"
                   + udp_packet(src_ip, dst_ip, int(dport), payload.encode()))
    elif kind == "ipproto":
        proto, dst, payload = args
        with socket.socket(socket.AF_INET, socket.SOCK_RAW, int(proto)) as s:
            s.sendto(payload.encode(), (dst, 0))
    elif kind == "flood":
        dst, port, count, rate, payload = args
        start = time.monotonic()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for n in range(int(count)):
                s.sendto(f"{payload}:{n}".encode(), (dst, int(port)))
                if n % 50 == 49:
                    time.sleep(max(0.0, start + (n + 1) / float(rate) - time.monotonic()))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(*sys.argv[1:])

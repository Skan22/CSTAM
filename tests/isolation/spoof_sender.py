"""Sends forged datagrams, for the real-cloud port-security check (runs on the attacking sandbox).

    python3 spoof_sender.py DST NEIGHBOUR UNUSED GATEWAY [ROUNDS]

One datagram each, to DST UDP 5140 (the port a sandbox is allowed to reach on a relay), every few
seconds: an honest one; one forged to come from NEIGHBOUR's address, one from an address nobody
holds, one from a gateway's address (raw IP packets); and one with a forged source MAC (a raw
Ethernet frame with this host's honest IP). The payload names the kind. A network with port
security delivers the honest ones and nothing else. Standard library only, needs root.
"""

import os
import random
import socket
import struct
import sys
import time

PORT = 5140


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return int(~total & 0xFFFF)


def ip_packet(src: str, dst: str, payload: bytes) -> bytes:
    udp = struct.pack("!HHHH", random.randint(20000, 60000), PORT, 8 + len(payload), 0) + payload
    head = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), random.randint(0, 65535), 0, 64, 17,
                       0, socket.inet_aton(src), socket.inet_aton(dst))
    head = head[:10] + struct.pack("!H", checksum(head)) + head[12:]
    return head + udp


def interface() -> str:
    for name in sorted(os.listdir("/sys/class/net")):
        if name != "lo":
            return name
    raise LookupError("no network interface")


def own_ip(dst: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((dst, PORT))
        return str(s.getsockname()[0])


def mac_of(ip: str) -> bytes:
    with open("/proc/net/arp") as f:
        lines = f.read().splitlines()[1:]
    for line in lines:
        fields = line.split()
        if fields[0] == ip and fields[3] != "00:00:00:00:00:00":
            return bytes.fromhex(fields[3].replace(":", ""))
    raise LookupError(ip)


def main() -> None:
    dst, neighbour, unused, gateway = sys.argv[1:5]
    rounds = int(sys.argv[5]) if len(sys.argv) > 5 else 8
    honest = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
    me = own_ip(dst)
    for _ in range(rounds):
        honest.sendto(b"honest", (dst, PORT))
        time.sleep(0.3)  # resolves the destination's MAC for the frame below
        print("sent honest", flush=True)
        for kind, src in (("spoof-ip-neighbour", neighbour), ("spoof-ip-unused", unused),
                          ("spoof-ip-gateway", gateway)):
            raw.sendto(ip_packet(src, dst, kind.encode()), (dst, 0))
            print(f"sent {kind} claiming {src}", flush=True)  # sendto raised nothing: it left
        try:
            frame_socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
            frame_socket.bind((interface(), 0))
            fake_mac = bytes.fromhex("fa163e000001")
            frame = mac_of(dst) + fake_mac + b"\x08\x00" + ip_packet(me, dst, b"spoof-mac")
            frame_socket.send(frame)
            frame_socket.close()
            print(f"sent spoof-mac from {fake_mac.hex(':')}", flush=True)
        except (LookupError, OSError) as exc:
            print(f"could not send spoof-mac: {exc}", flush=True)
        time.sleep(5)
    honest.sendto(b"end", (dst, PORT))
    print("done", flush=True)


if __name__ == "__main__":
    main()

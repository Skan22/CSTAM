"""Empties this network namespace's IPv4 connection-tracking table (what `conntrack -F` does),
so an entry left from a pass with fewer filters cannot let a later packet through as `established`."""

import socket
import struct

NETLINK_NETFILTER = 12
CT_DELETE = (1 << 8) | 2  # NFNL_SUBSYS_CTNETLINK, IPCTNL_MSG_CT_DELETE with no tuple: everything
REQUEST, ACK = 0x1, 0x4


def flush() -> None:
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_NETFILTER) as s:
        s.bind((0, 0))
        body = struct.pack("BBH", socket.AF_INET, 0, 0)
        s.send(struct.pack("=IHHII", 16 + len(body), CT_DELETE, REQUEST | ACK, 1, 0) + body)
        error = struct.unpack("=i", s.recv(4096)[16:20])[0]
        if error not in (0, -2):  # -ENOENT: the table was already empty
            raise OSError(-error, "conntrack flush failed")


if __name__ == "__main__":
    flush()

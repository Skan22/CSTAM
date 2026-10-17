"""A tiny authoritative DNS server that accepts RFC 2136 dynamic updates, for tests and the lab.

One zone, A records only, optional TSIG. It answers queries from what updates have put in it, so
a test can ask the same question a resolver would. Run it standalone:

    python tests/dnsd.py --listen 10.30.0.40:53 --zone cstam.felcloud.tn --tsig name=secretb64
"""

import argparse
import socketserver
import threading

import dns.exception
import dns.flags
import dns.message
import dns.name
import dns.opcode
import dns.rcode
import dns.rdataclass
import dns.rdatatype
import dns.rrset
import dns.tsigkeyring
import dns.update


class Zone:
    def __init__(self, origin: str) -> None:
        self.origin = dns.name.from_text(origin)
        self.records: dict[dns.name.Name, set[str]] = {}
        self.lock = threading.Lock()
        self.updates = 0

    def names(self) -> dict[str, list[str]]:
        with self.lock:
            return {n.to_text(): sorted(v) for n, v in self.records.items() if v}


class Handler(socketserver.BaseRequestHandler):
    server: "DnsServer"

    def handle(self) -> None:
        wire, sock = self.request
        try:
            msg = dns.message.from_wire(wire, keyring=self.server.keyring)
        except dns.message.UnknownTSIGKey:
            return self._refuse(sock, wire, dns.rcode.NOTAUTH)
        except dns.exception.DNSException:
            return None
        zone = self.server.zone
        reply = dns.message.make_response(msg)
        if msg.opcode() == dns.opcode.UPDATE:
            if self.server.keyring and not msg.had_tsig:
                reply.set_rcode(dns.rcode.REFUSED)
            elif not msg.question or msg.question[0].name != zone.origin:
                reply.set_rcode(dns.rcode.NOTAUTH)
            else:
                with zone.lock:
                    assert isinstance(msg, dns.update.UpdateMessage)
                    for rrset in msg.update:
                        if rrset.rdclass == dns.rdataclass.ANY:       # delete the RRset
                            zone.records.pop(rrset.name, None)
                        elif rrset.rdclass == dns.rdataclass.IN and rrset.rdtype == dns.rdatatype.A:
                            zone.records.setdefault(rrset.name, set())
                            zone.records[rrset.name] = {r.address for r in rrset}
                    zone.updates += 1
        elif msg.opcode() == dns.opcode.QUERY and msg.question:
            q = msg.question[0]
            with zone.lock:
                addrs = zone.records.get(q.name, set())
            if q.rdtype == dns.rdatatype.A and addrs:
                reply.answer.append(dns.rrset.from_text_list(q.name, 30, "IN", "A", sorted(addrs)))
            elif not addrs:
                reply.set_rcode(dns.rcode.NXDOMAIN)
            reply.flags |= dns.flags.AA
        else:
            reply.set_rcode(dns.rcode.NOTIMP)
        sock.sendto(reply.to_wire(), self.client_address)

    def _refuse(self, sock: object, wire: bytes, rcode: dns.rcode.Rcode) -> None:
        try:
            plain = dns.message.from_wire(wire, ignore_trailing=True, keyring=False)
        except dns.exception.DNSException:
            return
        reply = dns.message.make_response(plain)
        reply.set_rcode(rcode)
        sock.sendto(reply.to_wire(), self.client_address)  # type: ignore[attr-defined]


class DnsServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, listen: tuple[str, int], zone: Zone,
                 keyring: dict[dns.name.Name, bytes] | None) -> None:
        super().__init__(listen, Handler)
        self.zone, self.keyring = zone, keyring or None

    @property
    def port(self) -> int:
        return int(self.server_address[1])


def serve(host: str, port: int, zone: str, tsig: dict[str, str] | None = None) -> DnsServer:
    keyring = dns.tsigkeyring.from_text(tsig) if tsig else None
    server = DnsServer((host, port), Zone(zone), keyring)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:5353")
    p.add_argument("--zone", required=True)
    p.add_argument("--tsig", help="name=base64secret")
    a = p.parse_args()
    host, _, port = a.listen.rpartition(":")
    tsig = dict([a.tsig.split("=", 1)]) if a.tsig else None
    server = serve(host, int(port), a.zone, tsig)
    print(f"dnsd serving {a.zone} on {a.listen}", flush=True)
    threading.Event().wait()
    server.shutdown()


if __name__ == "__main__":
    main()

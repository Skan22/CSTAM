"""DNS records for team subdomains.

FelCloud has no DNS service (no Designate), so the default is a wildcard: `*.<domain>` is set once
to the gateway's floating IP and no per-team record ever exists. That is the cheapest design, and
`WildcardDns` is what it looks like to the sagas: every call is a no-op.

Where a zone does need per-team records, `Rfc2136Dns` publishes and withdraws them with dynamic
updates (RFC 2136), signed with a TSIG key. It works against BIND, Knot, PowerDNS and CoreDNS.
All calls are idempotent, so a saga step or the reconciler may repeat them freely.
"""

import base64
from collections.abc import Callable, Mapping
from typing import Protocol

import dns.exception
import dns.message
import dns.name
import dns.query
import dns.rcode
import dns.rdatatype
import dns.tsigkeyring
import dns.update


class DnsError(Exception):
    """The name server refused or did not answer."""


class Dns(Protocol):
    managed: bool  # False: the zone has no per-team records, so there is nothing to keep in step

    def ensure(self, host: str) -> None:
        """Make `host` resolve to the gateways."""
        ...

    def remove(self, host: str) -> None:
        """Make `host` stop resolving. Removing a name that has no record is not an error."""
        ...

    def exists(self, host: str) -> bool:
        """Whether the authoritative server has a record for `host`."""
        ...


class WildcardDns:
    managed = False

    def ensure(self, host: str) -> None:
        return None

    def remove(self, host: str) -> None:
        return None

    def exists(self, host: str) -> bool:
        return False


class FakeDns:
    """In-memory zone for tests, with fault injection."""

    managed = True

    def __init__(self, target: str = "203.0.113.10") -> None:
        self.target = target
        self.records: dict[str, str] = {}
        self._faults: dict[str, list[Exception]] = {}

    def inject(self, method: str, error: Exception | None = None, times: int = 1) -> None:
        """Make the next `times` calls to `method` raise."""
        err = error or DnsError(f"injected {method}")
        self._faults.setdefault(method, []).extend([err] * times)

    def reset(self) -> None:
        self.records.clear()
        self._faults.clear()

    def _enter(self, method: str) -> None:
        if self._faults.get(method):
            raise self._faults[method].pop(0)

    def ensure(self, host: str) -> None:
        self._enter("ensure")
        self.records[host] = self.target

    def remove(self, host: str) -> None:
        self._enter("remove")
        self.records.pop(host, None)

    def exists(self, host: str) -> bool:
        self._enter("exists")
        return host in self.records


class Rfc2136Dns:
    def __init__(self, *, server: str, zone: str, target: str, ttl: int = 30, port: int = 53,
                 tsig_name: str = "", tsig_secret_b64: str = "",
                 tsig_algorithm: str = "hmac-sha256", timeout: float = 3.0) -> None:
        self.server, self.port, self.target = server, port, target
        self.ttl, self.timeout = ttl, timeout
        self.zone = dns.name.from_text(zone)
        self.keyring = None
        self.keyname = None
        if tsig_name:
            base64.b64decode(tsig_secret_b64, validate=True)  # fail at start, not at first update
            self.keyring = dns.tsigkeyring.from_text({tsig_name: tsig_secret_b64})
            self.keyname = dns.name.from_text(tsig_name)
        self.algorithm = dns.name.from_text(tsig_algorithm)
        self.managed = True

    def _label(self, host: str) -> dns.name.Name:
        name = dns.name.from_text(host)
        if not name.is_subdomain(self.zone):
            raise DnsError(f"{host} is outside the zone {self.zone}")
        return name.relativize(self.zone)

    def _update(self, build: Callable[[dns.update.Update], object]) -> None:
        update = dns.update.Update(self.zone, keyring=self.keyring, keyname=self.keyname,
                                   keyalgorithm=self.algorithm)
        build(update)
        try:
            reply = dns.query.udp(update, self.server, port=self.port, timeout=self.timeout)
        except dns.exception.DNSException as exc:
            raise DnsError(f"dynamic update to {self.server} failed: {exc}") from exc
        rcode = reply.rcode()
        if rcode != dns.rcode.NOERROR:
            raise DnsError(f"{self.server} refused the update: {dns.rcode.to_text(rcode)}")

    def ensure(self, host: str) -> None:
        label = self._label(host)
        self._update(lambda u: u.replace(label, self.ttl, "A", self.target))

    def remove(self, host: str) -> None:
        label = self._label(host)
        self._update(lambda u: u.delete(label, "A"))

    def exists(self, host: str) -> bool:
        query = dns.message.make_query(dns.name.from_text(host), dns.rdatatype.A)
        try:
            reply = dns.query.udp(query, self.server, port=self.port, timeout=self.timeout)
        except dns.exception.DNSException as exc:
            raise DnsError(f"query to {self.server} failed: {exc}") from exc
        return reply.rcode() == dns.rcode.NOERROR and bool(reply.answer)


def from_env(env: Mapping[str, str], domain: str) -> Dns:
    """`IPO_DNS` is `wildcard` (default) or `rfc2136`, which also needs IPO_DNS_SERVER and
    IPO_DNS_TARGET (the address every team record points at; the gateways' floating IP)."""
    mode = env.get("IPO_DNS", "wildcard")
    if mode == "wildcard":
        return WildcardDns()
    if mode != "rfc2136":
        raise ValueError(f"IPO_DNS must be wildcard or rfc2136, not {mode!r}")
    for key in ("IPO_DNS_SERVER", "IPO_DNS_TARGET"):
        if not env.get(key):
            raise ValueError(f"IPO_DNS=rfc2136 needs {key}")
    host, _, port = env["IPO_DNS_SERVER"].partition(":")
    return Rfc2136Dns(
        server=host, port=int(port or 53), zone=env.get("IPO_DNS_ZONE", domain),
        target=env["IPO_DNS_TARGET"], ttl=int(env.get("IPO_DNS_TTL", "30")),
        tsig_name=env.get("IPO_DNS_TSIG_NAME", ""),
        tsig_secret_b64=env.get("IPO_DNS_TSIG_SECRET", ""),
        tsig_algorithm=env.get("IPO_DNS_TSIG_ALGORITHM", "hmac-sha256"))

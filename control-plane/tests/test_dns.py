"""The RFC 2136 adapter against a real DNS server speaking the protocol, with and without TSIG."""

import base64
from collections.abc import Iterator

import pytest

from ipo.adapters.dns import DnsError, Rfc2136Dns, WildcardDns, from_env
from tests.dnsd import DnsServer, serve

ZONE = "cstam.felcloud.tn"
SECRET = base64.b64encode(b"k" * 32).decode()
TARGET = "203.0.113.7"


@pytest.fixture
def server() -> Iterator[DnsServer]:
    s = serve("127.0.0.1", 0, ZONE, {"ipo-key": SECRET})
    yield s
    s.shutdown()


def client(server: DnsServer, secret: str = SECRET) -> Rfc2136Dns:
    return Rfc2136Dns(server="127.0.0.1", port=server.port, zone=ZONE, target=TARGET,
                      tsig_name="ipo-key", tsig_secret_b64=secret)


def test_a_record_is_published_resolves_and_is_withdrawn(server: DnsServer) -> None:
    dns = client(server)
    host = f"alpha.{ZONE}"
    assert not dns.exists(host)
    dns.ensure(host)
    assert dns.exists(host) and server.zone.names() == {f"alpha.{ZONE}.": [TARGET]}
    dns.ensure(host)  # idempotent: still one record
    assert server.zone.names() == {f"alpha.{ZONE}.": [TARGET]}
    dns.remove(host)
    assert not dns.exists(host) and server.zone.names() == {}
    dns.remove(host)  # removing what is not there is not an error


def test_other_teams_records_are_untouched(server: DnsServer) -> None:
    dns = client(server)
    for team in ("alpha", "beta", "gamma"):
        dns.ensure(f"{team}.{ZONE}")
    dns.remove(f"beta.{ZONE}")
    assert sorted(server.zone.names()) == [f"alpha.{ZONE}.", f"gamma.{ZONE}."]


def test_a_wrong_key_is_refused(server: DnsServer) -> None:
    bad = client(server, base64.b64encode(b"x" * 32).decode())
    with pytest.raises(DnsError):
        bad.ensure(f"alpha.{ZONE}")
    assert server.zone.names() == {}


def test_an_update_without_a_signature_is_refused(server: DnsServer) -> None:
    unsigned = Rfc2136Dns(server="127.0.0.1", port=server.port, zone=ZONE, target=TARGET)
    with pytest.raises(DnsError, match="REFUSED"):
        unsigned.ensure(f"alpha.{ZONE}")


def test_a_name_outside_the_zone_is_never_sent(server: DnsServer) -> None:
    with pytest.raises(DnsError, match="outside the zone"):
        client(server).ensure("alpha.example.org")
    assert server.zone.updates == 0


def test_an_unreachable_server_is_an_error_not_a_hang() -> None:
    dns = Rfc2136Dns(server="127.0.0.1", port=1, zone=ZONE, target=TARGET, timeout=0.3)
    with pytest.raises(DnsError):
        dns.ensure(f"alpha.{ZONE}")


def test_configuration_from_the_environment() -> None:
    assert isinstance(from_env({}, ZONE), WildcardDns)
    assert not from_env({"IPO_DNS": "wildcard"}, ZONE).managed
    dns = from_env({"IPO_DNS": "rfc2136", "IPO_DNS_SERVER": "10.30.0.40:5353",
                    "IPO_DNS_TARGET": TARGET, "IPO_DNS_TSIG_NAME": "k",
                    "IPO_DNS_TSIG_SECRET": SECRET}, ZONE)
    assert isinstance(dns, Rfc2136Dns) and dns.port == 5353 and dns.managed
    with pytest.raises(ValueError, match="IPO_DNS_TARGET"):
        from_env({"IPO_DNS": "rfc2136", "IPO_DNS_SERVER": "x"}, ZONE)
    with pytest.raises(ValueError, match="must be"):
        from_env({"IPO_DNS": "route53"}, ZONE)
    with pytest.raises(ValueError):
        from_env({"IPO_DNS": "rfc2136", "IPO_DNS_SERVER": "x", "IPO_DNS_TARGET": "y",
                  "IPO_DNS_TSIG_NAME": "k", "IPO_DNS_TSIG_SECRET": "not base64!!"}, ZONE)

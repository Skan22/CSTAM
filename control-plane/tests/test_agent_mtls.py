"""The control plane's agent client presents its certificate. A server that requires one, as the
agents do, must see it: with httpx 0.28 a CA given as a path made the client send none."""

import datetime as dt
import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ipo.controllers.compiler import ConfigVersion, HttpAgent


def _cert(cn: str, key: ec.EllipticCurvePrivateKey, issuer: x509.Name,
          signer: ec.EllipticCurvePrivateKey, *, ca: bool = False) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    b = (x509.CertificateBuilder().subject_name(subject)
         .issuer_name(issuer).public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(hours=1))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if not ca:
        san = x509.SubjectAlternativeName([x509.DNSName("localhost")])
        b = b.add_extension(san, critical=False)
    return b.sign(signer, hashes.SHA256())


def _write(path: Path, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey) -> tuple[str, str]:
    c, k = path.with_suffix(".crt"), path.with_suffix(".key")
    c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return str(c), str(k)


@pytest.fixture
def agent_server(tmp_path: Path) -> Iterator[tuple[str, dict[str, str], tuple[str, str], str]]:
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ipo-root")])
    ca = _cert("ipo-root", ca_key, ca_name, ca_key, ca=True)
    ca_file, _ = _write(tmp_path / "ca", ca, ca_key)
    skey, ckey = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    server_pair = _write(tmp_path / "server", _cert("gw-a", skey, ca_name, ca_key), skey)
    client = _cert("ipo-control-plane", ckey, ca_name, ca_key)
    client_pair = _write(tmp_path / "client", client, ckey)
    seen: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, body: dict[str, object]) -> None:
            peer = self.connection.getpeercert()
            seen[self.command] = dict(x[0] for x in peer["subject"])["commonName"]
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self._answer({"live_version": 7})

        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self._answer({"ok": True, "routed": True})

        def log_message(self, *args: object) -> None:
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=ca_file)
    ctx.load_cert_chain(*server_pair)
    ctx.verify_mode = ssl.CERT_REQUIRED  # as the agents do
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"https://localhost:{httpd.server_address[1]}", seen, client_pair, ca_file
    finally:
        httpd.shutdown()


def test_pushes_and_probes_carry_the_control_planes_certificate(
        agent_server: tuple[str, dict[str, str], tuple[str, str], str]) -> None:
    url, seen, client_pair, ca_file = agent_server
    agent = HttpAgent(url, cert=client_pair, ca=ca_file)
    ack = agent.put_config(ConfigVersion(7, "sha", "sig", "{}"))
    assert ack.ok and ack.live_version == 7
    assert agent.probe("a.x") == (True, "")
    assert seen == {"PUT": "ipo-control-plane", "POST": "ipo-control-plane"}


def test_without_a_certificate_the_agent_refuses_and_says_so(
        agent_server: tuple[str, dict[str, str], tuple[str, str], str]) -> None:
    url, _, _, ca_file = agent_server
    with pytest.raises(ConnectionError):
        HttpAgent(url, ca=ca_file).put_config(ConfigVersion(7, "sha", "sig", "{}"))

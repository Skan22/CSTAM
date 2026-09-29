"""The Go agent must accept exactly what this compiler produces.

The fixture is a config rendered and signed here with a fixed key (Ed25519 is deterministic), so
`gateway-agent/testdata/python_envelope.json` changes only when the wire format does. The Go
tests verify and validate it; this test fails if the compiler drifts from the fixture. Refresh it
with `IPO_UPDATE_FIXTURE=1 uv run pytest tests/test_agent_contract.py`.
"""

import base64
import json
import os
from pathlib import Path

from ipo.controllers.compiler import MIDDLEWARES, RouteRow, render
from ipo.domain.signing import Signer, sha256_hex

FIXTURE = Path(__file__).resolve().parents[2] / "gateway-agent/testdata/python_envelope.json"
SEED = base64.b64encode(bytes(range(32))).decode()


def build() -> dict[str, object]:
    signer = Signer.from_private_b64(SEED)
    routes = [
        RouteRow("alpha.felcloud.test", "10.20.0.11", 8080, ["secure-headers", "compress"]),
        RouteRow("beta.felcloud.test", "10.20.0.12", 8080, ["rate-limit"]),
        RouteRow("gamma.felcloud.test", "10.20.0.109", 8080, []),
    ]
    body = render(routes, domain="felcloud.test")
    digest = sha256_hex(body)
    return {
        "public_key": signer.public_b64,
        "cidr": "10.20.0.0/24",
        "middlewares": sorted(MIDDLEWARES),
        "routers": ["ipo-canary", "t-alpha-felcloud-test", "t-beta-felcloud-test",
                    "t-gamma-felcloud-test"],
        "envelope": {"version": 42, "sha256": digest, "body": body,
                     "signature": signer.sign(42, digest, body)},
    }


def test_fixture_matches_what_the_compiler_produces() -> None:
    fresh = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("IPO_UPDATE_FIXTURE"):
        FIXTURE.write_text(fresh)
    assert FIXTURE.read_text() == fresh, "run with IPO_UPDATE_FIXTURE=1 to refresh the Go fixture"

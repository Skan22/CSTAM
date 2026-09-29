"""The control plane's HttpAgent against the real Go agent pipeline (fake Traefik behind it).

Skipped when the Go toolchain is missing. This is the contract test for the HTTP status mapping:
4xx must come back as `Ack(ok=False)` (never retried) and a re-push of the live version as ok.
"""

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from ipo.controllers.compiler import ConfigVersion, HttpAgent, RouteRow, render
from ipo.domain.signing import Signer, sha256_hex

AGENT_DIR = Path(__file__).resolve().parents[2] / "gateway-agent"

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="Go toolchain not installed")

DOMAIN = "felcloud.test"


def version(signer: Signer, n: int, *routes: RouteRow) -> ConfigVersion:
    body = render(list(routes), domain=DOMAIN)
    digest = sha256_hex(body)
    return ConfigVersion(n, digest, signer.sign(n, digest, body), body)


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer.generate()


@pytest.fixture(scope="module")
def binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("go") / "dev-gateway"
    subprocess.run(["go", "build", "-o", str(out), "./cmd/dev-gateway"], cwd=AGENT_DIR,
                   check=True, capture_output=True)
    return out


@pytest.fixture
def agent(binary: Path, signer: Signer) -> Iterator[HttpAgent]:
    proc = subprocess.Popen([str(binary), "-pubkey", signer.public_b64], stdout=subprocess.PIPE,
                            text=True)
    try:
        assert proc.stdout
        line = proc.stdout.readline()
        assert line.startswith("LISTENING "), line
        yield HttpAgent("http://" + line.split()[1])
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_accepts_what_the_compiler_signs(agent: HttpAgent, signer: Signer) -> None:
    v1 = version(signer, 1, RouteRow("a.felcloud.test", "10.20.0.11", 8080, ["compress"]))
    assert agent.put_config(v1) == agent.put_config(v1)  # a retried push is an ack, not an error
    ack = agent.put_config(version(signer, 2, RouteRow("a.felcloud.test", "10.20.0.11", 8080, []),
                                   RouteRow("b.felcloud.test", "10.20.0.12", 8080, [])))
    assert ack.ok and ack.live_version == 2


def test_refusals_are_permanent_acks_that_report_the_live_version(
        agent: HttpAgent, signer: Signer) -> None:
    good = version(signer, 5, RouteRow("a.felcloud.test", "10.20.0.11", 8080, []))
    assert agent.put_config(good).ok

    replay = agent.put_config(version(signer, 4, RouteRow("z.felcloud.test", "10.20.0.19", 80, [])))
    assert not replay.ok and replay.live_version == 5 and "not newer" in (replay.error or "")

    outside = agent.put_config(version(signer, 6, RouteRow("x.felcloud.test", "10.99.0.5", 80, [])))
    assert not outside.ok and outside.live_version == 5 and "outside" in (outside.error or "")

    forged = Signer.generate()
    bad = agent.put_config(version(forged, 7, RouteRow("y.felcloud.test", "10.20.0.12", 80, [])))
    assert not bad.ok and bad.live_version == 5 and "signature" in (bad.error or "")


def test_fault_reaches_the_agent(agent: HttpAgent) -> None:
    agent.fault()  # raises unless the agent answers 200

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from chaos.world import World, boot
from load.burst import Burst


@pytest.fixture(scope="session")
def world() -> Iterator[World]:
    dsn = os.environ.get("IPO_LAB_DSN")
    if not dsn:
        pytest.skip("run through `uv run python -m load.run` (needs the namespace lab)")
    env = {"IPO_WORKERS": os.environ.get("IPO_LOAD_WORKERS", "48"),
           "IPO_FAKE_BOOT_SECONDS": os.environ.get("IPO_LOAD_BOOT_SECONDS", "0"),
           "IPO_FAKE_STATE": str(Path(os.environ["IPO_LAB_WORKDIR"]) / "fake-cloud.json")}
    w = boot(Path(os.environ["IPO_LAB_WORKDIR"]), dsn, env, dns=True)
    # The pool follows only the targets the tests set (test_pool_growth turns demand growth on).
    w.cp.api("PATCH", "/v1/settings", {"demand_window_seconds": 0})
    try:
        yield w
    finally:
        w.shutdown()


@pytest.fixture(scope="session")
def sql() -> Callable[[str], str]:
    """Run a statement against the lab's database (a way to age leases without waiting)."""
    def run(statement: str) -> str:
        out = subprocess.run(["psql", os.environ["IPO_LAB_DSN"], "-tA", "-c", statement],
                             check=True, capture_output=True, text=True)
        return out.stdout.strip()
    return run


@pytest.fixture(scope="session")
def emit() -> Callable[[str, dict[str, Any]], None]:
    """Print a measurement and append it to load-report.jsonl, which `load.report` turns into
    the benchmark tables."""
    def write(name: str, data: dict[str, Any]) -> None:
        line = {"scenario": name, **data}
        print("\n" + json.dumps(line))
        out = os.environ.get("IPO_LAB_WORKDIR")
        if out:
            with (Path(out).parent / "load-report.jsonl").open("a") as f:
                f.write(json.dumps(line) + "\n")
    return write


@pytest.fixture(scope="session")
def record(emit: Callable[[str, dict[str, Any]], None]) -> Callable[[str, Burst, dict[str, Any]], None]:
    def write(name: str, burst: Burst, extra: dict[str, Any]) -> None:
        emit(name, {**extra, **burst.summary()})
    return write

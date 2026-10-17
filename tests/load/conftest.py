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
    env = {"IPO_WORKERS": os.environ.get("IPO_LOAD_WORKERS", "8"),
           "IPO_FAKE_BOOT_SECONDS": os.environ.get("IPO_LOAD_BOOT_SECONDS", "0")}
    w = boot(Path(os.environ["IPO_LAB_WORKDIR"]), dsn, env, dns=True)
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
def record() -> Callable[[str, Burst, dict[str, Any]], None]:
    def write(name: str, burst: Burst, extra: dict[str, Any]) -> None:
        out = os.environ.get("IPO_LAB_WORKDIR")
        line = {"scenario": name, **extra, **burst.summary()}
        print("\n" + json.dumps(line))
        if out:
            with (Path(out).parent / "load-report.jsonl").open("a") as f:
                f.write(json.dumps(line) + "\n")
    return write

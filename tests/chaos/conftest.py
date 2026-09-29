import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from chaos.world import World, boot


@pytest.fixture(scope="session")
def world() -> Iterator[World]:
    dsn = os.environ.get("IPO_LAB_DSN")
    if not dsn:
        pytest.skip("run through `uv run python -m chaos.run` (needs the namespace lab)")
    w = boot(Path(os.environ["IPO_LAB_WORKDIR"]), dsn)
    try:
        yield w
    finally:
        w.shutdown()


@pytest.fixture
def pair(world: World) -> Iterator[World]:
    """The world, guaranteed healthy before the test and put back after it."""
    world.heal()
    try:
        yield world
    finally:
        world.heal()

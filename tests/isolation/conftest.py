import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from isolation import model
from isolation.lab import IsolationLab


@pytest.fixture(scope="session")
def lab() -> Iterator[IsolationLab]:
    if not os.environ.get("IPO_ISOLATION_LAB"):
        pytest.skip("run through `uv run python -m isolation.run` (needs the namespace lab)")
    platform, spec = model.load()
    lab = IsolationLab(Path(os.environ["IPO_LAB_WORKDIR"]), platform, spec)
    try:
        lab.build()
        yield lab
    finally:
        lab.close()


@pytest.fixture
def secured(lab: IsolationLab) -> IsolationLab:
    """Both layers on, as deployed."""
    lab.set_layers(host=True, fabric=True)
    return lab

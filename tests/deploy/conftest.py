"""Shared helpers: role variables as Ansible would resolve them, and the real programs that read
the rendered files (lab/fetch-tools.sh validators puts the pinned versions in the cache)."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from lab import render, tools

VALIDATORS = tools.CACHE / "validators"
SECRETS: dict[str, Any] = yaml.safe_load((render.ANSIBLE / "secrets.example.yaml").read_text())
QUADLETS = render.REPO / "quadlets"


def tool(name: str) -> Path:
    """A validator from the cache or the PATH; the test is skipped without it."""
    cached = VALIDATORS / name
    found = cached if cached.is_file() else shutil.which(name)
    if not found:
        pytest.skip(f"{name} not found (run `sh lab/fetch-tools.sh validators`)")
    return Path(found)


def hostvars(host: str, *roles: str, **extra: Any) -> dict[str, Any]:
    return render.host_vars(host, list(roles), extra={**SECRETS, **extra})

"""The two specs, validated before anything is created (plan M1: at the start of every run)."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

REPO = Path(__file__).resolve().parents[3]


def _ipo_net() -> Any:
    """The same module the Ansible roles and the isolation lab use to read security-groups.yaml."""
    path = REPO / "ansible" / "filter_plugins" / "ipo_net.py"
    s = importlib.util.spec_from_file_location("ipo_net", path)
    assert s and s.loader
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    return mod


ipo_net = _ipo_net()


def _load(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((REPO / f"{name}.yaml").read_text())
    schema = json.loads((REPO / f"{name}.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(doc)
    assert isinstance(doc, dict)
    return doc


def load() -> tuple[dict[str, Any], dict[str, Any]]:
    """(platform, security groups), or an exception naming what is wrong."""
    platform, groups = _load("platform"), _load("security-groups")
    problems = ipo_net.problems(groups, platform)
    if problems:
        raise ValueError("security-groups.yaml: " + "; ".join(problems))
    return platform, groups

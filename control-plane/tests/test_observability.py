"""The alert rules and dashboards only mean something if the metrics they name exist."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ipo import metrics

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "observability"
SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


def defined_metrics() -> set[str]:
    names = {
        m.describe()[0].name for m in vars(metrics).values()
        if hasattr(m, "describe") and hasattr(m, "_name")
    }
    agent = (ROOT / "gateway-agent/internal/metrics/metrics.go").read_text()
    names |= set(re.findall(r"ipo_agent_[a-z_]+", agent))
    return names


def base(name: str) -> str:
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return name.removesuffix(suffix)
    return name


def referenced(expr: str) -> set[str]:
    return set(re.findall(r"\bipo_[a-z_]+", expr))


def known(name: str, defined: set[str]) -> bool:
    return name in defined or base(name) in {base(d) for d in defined}


def alert_rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load((OBS / "alerts.yml").read_text())
    return [r for g in doc["groups"] for r in g["rules"]]


def test_every_alert_has_the_fields_an_operator_needs() -> None:
    rules = alert_rules()
    assert len(rules) >= 6
    names = [r["alert"] for r in rules]
    assert len(names) == len(set(names))
    for r in rules:
        assert r["expr"].strip() and r["for"] and r["labels"]["severity"] in ("page", "warn")
        assert r["annotations"]["summary"] and r["annotations"]["runbook"]


def test_alerts_only_use_metrics_that_exist() -> None:
    defined = defined_metrics()
    for r in alert_rules():
        for name in referenced(r["expr"]):
            assert known(name, defined), f"{r['alert']} uses unknown metric {name}"


def test_dashboards_are_valid_and_only_use_metrics_that_exist() -> None:
    defined = defined_metrics()
    files = sorted((OBS / "dashboards").glob("*.json"))
    assert {f.stem for f in files} >= {"overview", "gateways", "provisioning", "ipam"}
    for f in files:
        d = json.loads(f.read_text())
        assert d["title"] and d["uid"] == f.stem and d["panels"]
        ids = [p["id"] for p in d["panels"]]
        assert len(ids) == len(set(ids))
        for p in d["panels"]:
            for t in p["targets"]:
                for name in referenced(t["expr"]):
                    assert known(name, defined), f"{f.name}: {p['title']} uses {name}"


def test_defined_metrics_include_the_saga_and_provision_series() -> None:
    d = defined_metrics()
    assert {"ipo_saga_step_duration_seconds", "ipo_provision_duration_seconds"} <= d

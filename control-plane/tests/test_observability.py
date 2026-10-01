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
    return set(re.findall(r"\bipo_[a-z0-9_]+", expr))


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
    assert {f.stem for f in files} >= {
        "overview", "gateways", "provisioning", "ipam", "team-traffic"}
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


def test_dashboards_on_disk_match_the_generator() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_dashboards", OBS / "build_dashboards.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for uid, (title, panels) in mod.DASHBOARDS.items():
        want = mod.dashboard(uid, title, panels, mod.VARIABLES.get(uid, ()))
        assert json.loads((OBS / "dashboards" / f"{uid}.json").read_text()) == want, uid
    spa = json.loads((ROOT / "dashboard/src/generated/panels.json").read_text())
    assert spa == mod.embeds()


def test_the_team_dashboard_has_a_team_variable_and_a_split_brain_panel_exists() -> None:
    team = json.loads((OBS / "dashboards/team-traffic.json").read_text())
    assert [v["name"] for v in team["templating"]["list"]] == ["team"]
    assert all("$team" in p["targets"][0]["expr"] for p in team["panels"])
    gateways = json.loads((OBS / "dashboards/gateways.json").read_text())
    assert any("ipo_split_brain_total" in p["targets"][0]["expr"] for p in gateways["panels"])


def test_every_embedded_panel_exists_in_its_dashboard() -> None:
    spa = json.loads((ROOT / "dashboard/src/generated/panels.json").read_text())
    assert spa["panels"]
    for e in spa["panels"]:
        d = json.loads((OBS / "dashboards" / f"{e['uid']}.json").read_text())
        match = [p for p in d["panels"] if p["id"] == e["id"]]
        assert match and match[0]["title"] == e["title"]
        assert e["team_var"] == bool(d["templating"]["list"])

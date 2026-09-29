"""Generate the Grafana dashboards in ./dashboards. Run: python3 observability/build_dashboards.py"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "dashboards"


def panel(pid: int, title: str, expr: str, kind: str = "timeseries", legend: str = "",
          x: int = 0, y: int = 0) -> dict:
    target = {"expr": expr, "refId": "A"}
    if legend:
        target["legendFormat"] = legend
    return {"id": pid, "title": title, "type": kind, "targets": [target],
            "datasource": {"type": "prometheus", "uid": "prometheus"},
            "gridPos": {"x": x, "y": y, "w": 12, "h": 8}}


def dashboard(uid: str, title: str, panels: list[tuple]) -> dict:
    built = [panel(i + 1, *p, x=(i % 2) * 12, y=(i // 2) * 8) for i, p in enumerate(panels)]
    return {"uid": uid, "title": title, "schemaVersion": 39, "version": 1, "refresh": "5s",
            "time": {"from": "now-30m", "to": "now"}, "tags": ["ipo"], "panels": built}


DASHBOARDS = {
    "overview": ("IPO Overview", [
        ("Teams by state", "ipo_teams", "timeseries", "{{state}}"),
        ("Warm pool size", "ipo_pool_size", "stat"),
        ("Pool target", "ipo_pool_target", "stat"),
        ("Config version per gateway", "ipo_config_live_version", "stat", "{{gateway}}"),
        ("Free addresses", 'ipo_ip_addresses{state="free"}', "stat"),
        ("Active alerts", 'count(ALERTS{alertstate="firing", alertname=~"IPO.*"}) or vector(0)',
         "stat"),
    ]),
    "gateways": ("IPO Gateways", [
        ("Config version per gateway", "ipo_config_live_version", "timeseries", "{{gateway}}"),
        ("Agent config version", "ipo_agent_config_version", "timeseries"),
        ("Configs applied", "increase(ipo_agent_config_applied_total[5m])", "timeseries"),
        ("Configs rejected by reason",
         "sum by (reason) (increase(ipo_agent_config_rejected_total[5m]))", "timeseries",
         "{{reason}}"),
        ("Rollbacks", "increase(ipo_agent_rollbacks_total[5m])", "timeseries"),
        ("VRRP transitions", "increase(ipo_agent_vrrp_transitions_total[5m])", "timeseries"),
        ("Reload duration p95",
         "histogram_quantile(0.95, sum by (le) (rate(ipo_agent_reload_duration_seconds_bucket[5m])))",
         "timeseries"),
    ]),
    "provisioning": ("IPO Provisioning", [
        ("Provision time p95 by path",
         "histogram_quantile(0.95, sum by (le, path) "
         "(rate(ipo_provision_duration_seconds_bucket[10m])))", "timeseries", "{{path}}"),
        ("Saga step time p95",
         "histogram_quantile(0.95, sum by (le, step) "
         "(rate(ipo_saga_step_duration_seconds_bucket[10m])))", "timeseries", "{{step}}"),
        ("Jobs by outcome", "sum by (kind, outcome) (increase(ipo_saga_jobs_total[10m]))",
         "timeseries", "{{kind}} {{outcome}}"),
        ("API latency p95 by route",
         "histogram_quantile(0.95, sum by (le, route) "
         "(rate(ipo_api_request_duration_seconds_bucket[5m])))", "timeseries", "{{route}}"),
    ]),
    "ipam": ("IPO IPAM", [
        ("Addresses by state", "ipo_ip_addresses", "timeseries", "{{state}}"),
        ("Quarantined", 'ipo_ip_addresses{state="quarantined"}', "stat"),
        ("Reconciler repairs by rule",
         "sum by (rule) (increase(ipo_reconciler_repairs_total[10m]))", "timeseries", "{{rule}}"),
        ("Warm pool", "ipo_pool_size", "timeseries"),
    ]),
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for uid, (title, panels) in DASHBOARDS.items():
        (OUT / f"{uid}.json").write_text(json.dumps(dashboard(uid, title, panels), indent=2) + "\n")

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


TEAM_VAR = {
    "name": "team", "label": "Team", "type": "query", "multi": True, "includeAll": True,
    "datasource": {"type": "prometheus", "uid": "prometheus"},
    "query": {"query": "label_values(ipo_team_requests_5m, team)", "refId": "team"},
    "refresh": 2, "current": {"text": "All", "value": "$__all"},
}


def dashboard(uid: str, title: str, panels: list[tuple], variables: tuple = ()) -> dict:
    built = [panel(i + 1, *p, x=(i % 2) * 12, y=(i // 2) * 8) for i, p in enumerate(panels)]
    return {"uid": uid, "title": title, "schemaVersion": 39, "version": 1, "refresh": "5s",
            "time": {"from": "now-30m", "to": "now"}, "tags": ["ipo"], "panels": built,
            "templating": {"list": list(variables)}}


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
        ("Gateways reporting MASTER (should be 1)",
         "max(ipo_gateway_masters) or sum(ipo_agent_vrrp_master)", "stat"),
        ("MASTER per agent", "ipo_agent_vrrp_master", "timeseries"),
        ("Split brains seen (1h)", "increase(ipo_split_brain_total[1h])", "stat"),
        ("Gateways reporting MASTER, control plane view", "ipo_gateway_masters", "timeseries"),
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
    "team-traffic": ("IPO Team Traffic", [
        ("Requests by team, last 5 min",
         'sum by (team) (ipo_team_requests_5m{team=~"$team"})', "timeseries", "{{team}}"),
        ("Responses by class",
         'sum by (class) (ipo_team_requests_5m{team=~"$team"})', "timeseries", "{{class}}"),
        ("5xx responses", 'sum(ipo_team_requests_5m{team=~"$team", class="5xx"})', "stat"),
        ("Mean latency (ms)",
         'avg(ipo_team_latency_ms_5m{team=~"$team"})', "stat"),
        ("Latency by team (ms)", 'ipo_team_latency_ms_5m{team=~"$team"}', "timeseries",
         "{{team}}"),
    ]),
}

# Panels the SPA's Metrics page may embed, by dashboard uid and panel title.
EMBEDS = {
    "overview": ["Warm pool size", "Config version per gateway"],
    "gateways": ["Gateways reporting MASTER (should be 1)", "Split brains seen (1h)",
                 "Gateways reporting MASTER, control plane view"],
    "team-traffic": ["Requests by team, last 5 min", "Responses by class", "Latency by team (ms)"],
}
VARIABLES = {"team-traffic": (TEAM_VAR,)}


def embeds() -> dict:
    out = []
    for uid, titles in EMBEDS.items():
        dash, panels = DASHBOARDS[uid]
        ids = {p[0]: i + 1 for i, p in enumerate(panels)}
        for title in titles:
            out.append({"uid": uid, "dashboard": dash, "id": ids[title], "title": title,
                        "team_var": uid in VARIABLES})
    return {"panels": out}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for uid, (title, panels) in DASHBOARDS.items():
        (OUT / f"{uid}.json").write_text(json.dumps(dashboard(uid, title, panels, VARIABLES.get(uid, ())), indent=2) + "\n")
    spa = Path(__file__).parents[1] / "dashboard/src/generated"
    spa.mkdir(exist_ok=True)
    (spa / "panels.json").write_text(json.dumps(embeds(), indent=2) + "\n")

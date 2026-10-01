"""The dashboard's Grafana embedding against a real Grafana behind the real front: both started
from the configurations the roles render, on one origin as in production."""

import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from deploy.conftest import SECRETS, hostvars, tool
from deploy.test_configs import free_port, self_signed, write
from lab import render

HOMEPATH = Path("/usr/share/grafana")
PANELS = json.loads((render.REPO / "dashboard/src/generated/panels.json").read_text())["panels"]
PASSWORD = SECRETS["ipo_secrets"]["grafana_admin_password"]


def wait(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(url, verify=False, timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, f"{url} never answered"
        time.sleep(0.3)


@pytest.fixture(scope="module")
def front(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    grafana, caddy = tool("grafana"), tool("caddy")
    if not HOMEPATH.is_dir():
        pytest.skip(f"no Grafana home at {HOMEPATH}")
    tmp = tmp_path_factory.mktemp("grafana")
    web, gport = free_port(), free_port()
    base = f"https://127.0.0.1:{web}"

    v = hostvars("cp-2", "observability", observability_grafana_root_url=f"{base}/grafana/")
    for t in ("grafana.ini", "provisioning/datasources/ipo.yml", "provisioning/dashboards/ipo.yml"):
        write(tmp / t, render.render("observability", f"grafana/{t}.j2", **v))
    prov = tmp / "provisioning/dashboards/ipo.yml"
    prov.write_text(prov.read_text().replace("/var/lib/ipo-dashboards",
                                             str(render.REPO / "observability/dashboards")))
    gproc = subprocess.Popen(
        [str(grafana), "server", "--homepath", str(HOMEPATH), "--config", str(tmp / "grafana.ini"),
         # cfg:default.* sets defaults the file overrides; cfg:section.key overrides the file.
         f"cfg:default.paths.data={tmp / 'data'}", f"cfg:default.paths.logs={tmp / 'logs'}",
         f"cfg:default.paths.plugins={tmp / 'plugins'}",
         f"cfg:default.paths.provisioning={tmp / 'provisioning'}",
         "cfg:server.http_addr=127.0.0.1", f"cfg:server.http_port={gport}"],
        env={**os.environ, "GF_SECURITY_ADMIN_PASSWORD": PASSWORD},
        stdout=open(tmp / "grafana.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115

    self_signed(tmp)
    cv = hostvars("cp-1", "control_plane", ipo_mgmt_ip="127.0.0.1", ipo_pki_dir=str(tmp / "pki"),
                  control_plane_web_port=web, control_plane_api_port=free_port(),
                  control_plane_health_port=free_port(), control_plane_grafana_upstream=f"127.0.0.1:{gport}")
    write(tmp / "dashboard/index.html", "<!doctype html><title>IPO</title>")
    caddyfile = write(tmp / "Caddyfile", render.render("control_plane", "Caddyfile.j2", **cv)
                      .replace("/srv/dashboard", str(tmp / "dashboard")))
    cproc = subprocess.Popen([str(caddy), "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
                             stdout=open(tmp / "caddy.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115
    try:
        wait(f"http://127.0.0.1:{gport}/grafana/api/health", 90)
        wait(f"{base}/grafana/api/health")
        yield {"base": base, "direct": f"http://127.0.0.1:{gport}", "tmp": tmp}
    finally:
        for p in (cproc, gproc):
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


@pytest.fixture(scope="module")
def session(front: dict[str, Any]) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=front["base"], verify=False, timeout=10) as c:
        r = c.post("/grafana/login", json={"user": "admin", "password": PASSWORD})
        assert r.status_code == 200, r.text
        yield c


def test_grafana_lets_itself_be_framed_and_the_front_allows_only_its_own_origin(
        front: dict[str, Any]) -> None:
    direct = httpx.get(f"{front['direct']}/grafana/login")
    assert direct.status_code == 200 and "x-frame-options" not in direct.headers  # allow_embedding
    via_front = httpx.get(f"{front['base']}/grafana/login", verify=False)
    assert via_front.headers["x-frame-options"] == "SAMEORIGIN"


def test_a_login_through_the_front_sets_a_secure_lax_cookie(front: dict[str, Any]) -> None:
    r = httpx.post(f"{front['base']}/grafana/login", json={"user": "admin", "password": PASSWORD},
                   verify=False)
    assert r.status_code == 200
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("grafana_session="))
    assert "Secure" in cookie and "SameSite=Lax" in cookie


def test_anonymous_visitors_are_sent_to_the_login_on_the_same_origin(front: dict[str, Any]) -> None:
    r = httpx.get(f"{front['base']}/grafana/d-solo/overview/ipo-overview?panelId=1", verify=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/grafana/login")


def test_every_panel_the_dashboard_embeds_is_provisioned(session: httpx.Client) -> None:
    for uid in {p["uid"] for p in PANELS}:
        r = session.get(f"/grafana/api/dashboards/uid/{uid}")
        assert r.status_code == 200, uid
        panels = {p["id"]: p["title"] for p in r.json()["dashboard"]["panels"]}
        for p in (p for p in PANELS if p["uid"] == uid):
            assert panels.get(p["id"]) == p["title"], (uid, p)
        if any(p["team_var"] for p in PANELS if p["uid"] == uid):
            names = [t["name"] for t in r.json()["dashboard"]["templating"]["list"]]
            assert "team" in names


def test_a_panel_url_the_dashboard_builds_is_served(session: httpx.Client) -> None:
    p = PANELS[0]
    r = session.get(f"/grafana/d-solo/{p['uid']}/x?orgId=1&panelId={p['id']}&theme=dark")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_the_datasources_the_dashboards_use_exist(session: httpx.Client) -> None:
    for uid in ("prometheus", "loki", "tempo"):
        assert session.get(f"/grafana/api/datasources/uid/{uid}").status_code == 200, uid


def test_prometheus_can_scrape_grafana_where_its_config_says(front: dict[str, Any]) -> None:
    r = httpx.get(f"{front['direct']}/grafana/metrics")
    assert r.status_code == 200 and "grafana_" in r.text

"""The one setting the SPA needs from the server: where Grafana is."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ipo.api import auth
from ipo.api.app import AppDeps, create_app, grafana_url
from ipo.settings import Settings
from tests import test_api
from tests.test_api import SECRET, Api

api = test_api.api


def test_grafana_is_off_unless_configured(api: Api) -> None:
    r = api.client.get("/v1/ui", headers=api.h("viewer"))
    assert r.status_code == 200 and r.json() == {"grafana_url": ""}


def test_the_configured_grafana_url_reaches_staff(api: Api) -> None:
    def never() -> Any:
        raise AssertionError("/v1/ui does not touch the database")

    app = create_app(AppDeps(never, Settings(), jwt_secret=SECRET,
                             grafana_url="https://grafana.example/"))
    # The token check needs no database either: a signed token is all it reads.
    token = auth.issue(SECRET, 60, "v@x.example", "viewer")
    r = TestClient(app).get("/v1/ui", headers={"Authorization": f"Bearer {token}"})
    assert r.json() == {"grafana_url": "https://grafana.example"}


def test_the_ui_config_needs_a_login(api: Api) -> None:
    assert api.client.get("/v1/ui").status_code == 401


@pytest.mark.parametrize("raw,clean", [
    ("", ""), ("  ", ""), ("https://g.example", "https://g.example"),
    ("http://10.0.0.5:3000/", "http://10.0.0.5:3000"),
    ("https://g.example/grafana/", "https://g.example/grafana"),
    ("/grafana/", "/grafana"), ("/grafana", "/grafana"),
])
def test_grafana_url_is_trimmed_and_normalised(raw: str, clean: str) -> None:
    assert grafana_url(raw) == clean


@pytest.mark.parametrize("raw", [
    "javascript:alert(1)", "ftp://g.example", "//g.example", "g.example",
    "https://g.example/?x=1", "https://g.example/#x", "https://user:pw@g.example",
    "/", "/\\evil.example", "/grafana?x=1", "/grafana#x", "/ grafana",
])
def test_anything_but_a_plain_http_url_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        grafana_url(raw)

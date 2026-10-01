"""Every unit through Podman's own Quadlet generator (Debian 13's 5.4.2, what the hosts run), and
the rules quadlets/README.md promises."""

import configparser
import os
import re
import shlex
import subprocess

import pytest
import yaml

from deploy.conftest import QUADLETS, tool
from lab import render

CONTAINERS = sorted(QUADLETS.glob("*.container"))
# Images that could not be checked for a shell or HTTP client (see the units' comments).
NO_HEALTH_CHECK = {"loki", "tempo", "podman-exporter"}


def generated() -> dict[str, str]:
    out = subprocess.run([str(tool("quadlet")), "-dryrun", "-user"], capture_output=True, text=True,
                         env={**os.environ, "QUADLET_UNIT_DIRS": str(QUADLETS)}, check=True)
    units, name = {}, None
    for line in out.stdout.splitlines():
        if m := re.fullmatch(r"---(.+)---", line):
            name = m.group(1)
            units[name] = ""
        elif name:
            units[name] += line + "\n"
    assert "error" not in out.stderr.lower(), out.stderr
    return units


def unit(path: os.PathLike[str]) -> configparser.RawConfigParser:
    cfg = configparser.RawConfigParser(strict=False, interpolation=None)
    cfg.optionxform = str  # type: ignore[assignment,method-assign]
    cfg.read(path)
    return cfg


def test_quadlet_generates_a_service_for_every_unit() -> None:
    units = generated()
    expected = {p.stem + ("-volume" if p.suffix == ".volume" else "") + ".service"
                for p in QUADLETS.iterdir() if p.suffix in (".container", ".volume")}
    assert set(units) == expected


def test_every_health_check_reaches_podman_as_one_well_quoted_command() -> None:
    """Quadlet re-quotes HealthCmd; a value with nested quotes came out unbalanced once."""
    for name, body in generated().items():
        start = next((ln for ln in body.splitlines() if ln.startswith("ExecStart=")), "")
        argv = shlex.split(start.removeprefix("ExecStart="))
        if "--health-cmd" in argv:
            cmd = argv[argv.index("--health-cmd") + 1]
            assert shlex.split(cmd), name
            assert '"' not in cmd and "'" not in cmd, f"{name}: {cmd}"


@pytest.mark.parametrize("path", CONTAINERS, ids=lambda p: p.stem)
def test_container_rules(path: os.PathLike[str]) -> None:
    cfg = unit(path)
    c = cfg["Container"]
    name = os.path.basename(path).removesuffix(".container")
    assert c["ContainerName"] == name
    image = c["Image"]
    assert re.search(r":[\w.-]+$", image) and not image.endswith(":latest"), image
    assert c["Network"] == "host"
    oneshot = cfg.get("Service", "Type", fallback="") == "oneshot"
    if oneshot:
        return
    assert c.get("AutoUpdate") == "registry"
    assert cfg["Service"]["Restart"] == "always"
    assert cfg["Install"]["WantedBy"] == "default.target"
    if name in NO_HEALTH_CHECK:
        assert "HealthCmd" not in c and "Notify" not in c
    else:
        assert c["HealthCmd"] and c["Notify"] == "healthy"


def test_images_the_roles_pin_match_the_units() -> None:
    traefik = yaml.safe_load((render.ROLES / "traefik" / "defaults" / "main.yml").read_text())
    assert f"traefik:v{traefik['traefik_version']}" in (QUADLETS / "traefik.container").read_text()
    pg = yaml.safe_load((render.ROLES / "postgres" / "defaults" / "main.yml").read_text())
    for f in ("postgres.container", "postgres-backup.container"):
        assert f"Image={pg['postgres_image']}" in (QUADLETS / f).read_text()

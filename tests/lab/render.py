"""Renders the Ansible role templates outside Ansible, so tests run the real config files."""

from pathlib import Path
from typing import Any

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
ROLES = REPO / "ansible" / "roles"


def platform() -> dict[str, Any]:
    cfg = yaml.safe_load((REPO / "platform.yaml").read_text())
    assert isinstance(cfg, dict)
    return cfg


def render(role: str, template: str, **variables: Any) -> str:
    """Render ansible/roles/<role>/templates/<template> the way Ansible's template module does."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLES / role / "templates"),
        undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.get_template(template).render(**variables)


def keepalived_vars(plat: dict[str, Any], host: str, *, interface: str, bin_dir: Path,
                    state_file: Path, fault_file: Path, script_user: str = "root") -> dict[str, Any]:
    """The keepalived role's variables for `host`, computed the way its defaults do."""
    net, vrrp = plat["network"], plat["vrrp"]
    primary = host == "gw-a"
    peer = "gw-b" if primary else "gw-a"
    return {
        "keepalived_router_id": host,
        "keepalived_router_vrid": net.get("vrrp_router_id", 51),
        "keepalived_priority": vrrp["priority_a"] if primary else vrrp["priority_b"],
        "keepalived_advert_int": vrrp["advert_interval_seconds"],
        "keepalived_check_interval": vrrp["check_interval_seconds"],
        "keepalived_check_timeout": 2,
        "keepalived_check_fall": 2,
        "keepalived_check_rise": 2,
        "keepalived_interface": interface,
        "keepalived_src_ip": net["hosts"][host]["edge"],
        "keepalived_peer_ip": net["hosts"][peer]["edge"],
        "keepalived_vip": net["vip"],
        "keepalived_vip_prefix": net["edge_cidr"].split("/")[1],
        "keepalived_script_user": script_user,
        # keepalived's script-security check walks up to / and wants root owners; in a user
        # namespace the real root directory shows as nobody, so the lab cannot satisfy it.
        "keepalived_script_security": False,
        "keepalived_check_script": str(bin_dir / "ipo-check-traefik"),
        "keepalived_notify": str(bin_dir / "ipo-vrrp-notify"),
        "keepalived_state_file": str(state_file),
        "keepalived_fault_file": str(fault_file),
    }


def traefik_vars(*, web_port: int, api_address: str, access_log: Path, live: Path,
                 acme_email: str = "") -> dict[str, Any]:
    return {
        "traefik_web_port": web_port, "traefik_websecure_port": 443,
        "traefik_api_address": api_address, "traefik_access_log": str(access_log),
        "traefik_log_level": "INFO", "traefik_live_config": str(live),
        "traefik_acme_email": acme_email, "traefik_acme_storage": "/var/lib/traefik/acme.json",
    }

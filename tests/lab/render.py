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
    env.filters.update(_filters())
    env.globals["lookup"] = _lookup
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


ANSIBLE = REPO / "ansible"


def _filters() -> dict[str, Any]:
    """ipo_net's filters plus the Ansible ones the roles use."""
    import hashlib
    import importlib.util
    import json
    import re
    spec = importlib.util.spec_from_file_location("ipo_net", ANSIBLE / "filter_plugins" / "ipo_net.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    filters: dict[str, Any] = mod.FilterModule().filters()
    filters.update({
        "combine": lambda a, *more: {k: v for d in (a, *more) for k, v in d.items()},
        "from_yaml": yaml.safe_load, "from_json": json.loads,
        "to_json": lambda v: json.dumps(v), "to_yaml": lambda v: yaml.safe_dump(v),
        "regex_replace": lambda s, pat, rep="": re.sub(pat, rep, str(s)),
        "extract": lambda key, container: container[key],
        "intersect": lambda a, b: [x for x in a if x in b],
        "difference": lambda a, b: [x for x in a if x not in b],
        "dict2items": lambda d: [{"key": k, "value": v} for k, v in d.items()],
        "hash": lambda s, algo="sha1": hashlib.new(algo, str(s).encode()).hexdigest(),
        "basename": lambda s: str(s).rsplit("/", 1)[-1],
        "dirname": lambda s: str(s).rsplit("/", 1)[0],
        "bool": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
    })
    return filters


def _lookup(plugin: str, *args: str, **_: Any) -> str:
    if plugin in ("file", "ansible.builtin.file"):
        return Path(args[0]).read_text().rstrip("\n")
    if plugin in ("env", "ansible.builtin.env"):
        import os
        return os.environ.get(args[0], "")
    raise NotImplementedError(f"lookup {plugin!r} is not emulated")


def environment(**kw: Any) -> jinja2.Environment:
    from jinja2.nativetypes import NativeEnvironment
    env = NativeEnvironment(undefined=jinja2.StrictUndefined, **kw)
    env.filters.update(_filters())
    env.globals["lookup"] = _lookup
    return env


class _Lazy(dict[str, Any]):
    """Variables resolved on first use, recursively, as Ansible does: Jinja looks names up in a
    shared context through __getitem__, so a variable is templated only when something reads it,
    after the variables it reads. A string that is one expression becomes its native value."""

    def __init__(self, raw: dict[str, Any], env: Any) -> None:
        super().__init__(raw)
        self._env = env
        self._done: dict[str, Any] = {}
        self._busy: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        if key not in self._done:
            if key in self._busy:
                raise ValueError(f"{key} refers to itself")
            self._busy.add(key)
            try:
                self._done[key] = self._template(super().__getitem__(key))
            finally:
                self._busy.discard(key)
        return self._done[key]

    def _template(self, value: Any) -> Any:
        if isinstance(value, str) and "{{" in value:
            t = self._env.from_string(value)
            return self._env.concat(t.root_render_func(t.new_context(self, shared=True)))
        if isinstance(value, list):
            return [self._template(v) for v in value]
        if isinstance(value, dict):
            return {k: self._template(v) for k, v in value.items()}
        return value


def resolve(raw: dict[str, Any]) -> dict[str, Any]:
    """Every variable resolved (see _Lazy). One that cannot be (a file Pulumi has not written
    yet, say) becomes an undefined that fails only the templates that use it."""
    env = environment()
    lazy = _Lazy({**raw, "lookup": _lookup}, env)
    out: dict[str, Any] = {}
    for k in raw:
        try:
            out[k] = lazy[k]
        except Exception as e:  # noqa: BLE001 - kept as an undefined that explains itself
            out[k] = jinja2.StrictUndefined(hint=f"{k} could not be resolved: {e}")
    return out


def inventory_group(host: str) -> str:
    """The inventory group a platform host is in (inventory/static.yml)."""
    inv = yaml.safe_load((ANSIBLE / "inventory" / "static.yml").read_text())
    for group, body in inv["all"]["children"].items():
        if host in body["hosts"]:
            return str(group)
    raise KeyError(host)


def inventory() -> dict[str, list[str]]:
    inv = yaml.safe_load((ANSIBLE / "inventory" / "static.yml").read_text())
    groups = {g: list(body["hosts"]) for g, body in inv["all"]["children"].items()}
    groups["all"] = [h for hosts in groups.values() for h in hosts]
    return groups


def facts(plat: dict[str, Any], host: str) -> dict[str, Any]:
    """Enough of `ansible_facts` for the roles: one NIC per network, in Nova's order."""
    nics = {f"ens{3 + i}": addr for i, addr in enumerate(plat["network"]["hosts"].get(host, {}).values())}
    return {"interfaces": ["lo", *nics], "lo": {"ipv4": {"address": "127.0.0.1"}},
            **{n: {"ipv4": {"address": a}} for n, a in nics.items()},
            "os_family": "Debian", "architecture": "x86_64", "hostname": host}


def host_vars(host: str, roles: list[str], *, plat: dict[str, Any] | None = None,
              spec: dict[str, Any] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything a template on `host` sees, with Ansible's precedence: the roles' defaults, then
    inventory group_vars (all, then the host's group), then `extra` (like -e)."""
    raw: dict[str, Any] = {}
    for role in roles:
        raw.update(yaml.safe_load((ROLES / role / "defaults" / "main.yml").read_text()) or {})
    groups = inventory()
    names = [g for g, hosts in groups.items() if host in hosts and g != "all"]
    for group in ["all", *names]:
        f = ANSIBLE / "inventory" / "group_vars" / f"{group}.yml"
        if f.exists():
            raw.update(yaml.safe_load(f.read_text()) or {})
    plat = plat or platform()
    raw.update(inventory_hostname=host, inventory_dir=str(ANSIBLE / "inventory"), groups=groups,
               group_names=names, ansible_facts=facts(plat, host), ipo_platform=plat,
               ipo_security_groups=spec or yaml.safe_load((REPO / "security-groups.yaml").read_text()))
    raw.update(extra or {})
    return resolve(raw)


def role_vars(role: str, host: str, *, plat: dict[str, Any] | None = None,
              spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """A role's defaults overlaid with the host's group_vars, resolved for `host`."""
    return host_vars(host, [role], plat=plat, spec=spec)

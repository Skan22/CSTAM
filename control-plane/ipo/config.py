"""Load and validate platform.yaml, the single source of truth for every layer."""

import ipaddress
import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "platform.schema.json"

_formats = FormatChecker()


@_formats.checks("ipv4")
def _is_ipv4(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return True


@_formats.checks("ipv4-cidr")
def _is_ipv4_cidr(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        ipaddress.IPv4Network(value)
    except ValueError:
        return False
    return True


class PlatformConfigError(ValueError):
    pass


def validate_platform(cfg: dict[str, Any]) -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()), format_checker=_formats)
    errors = sorted(validator.iter_errors(cfg), key=lambda e: list(e.absolute_path))
    if errors:
        raise PlatformConfigError("; ".join(_describe(e) for e in errors))

    pool = cfg["ip_pool"]
    net = ipaddress.IPv4Network(pool["cidr"])
    first = ipaddress.IPv4Address(pool["first"])
    last = ipaddress.IPv4Address(pool["last"])
    if first > last:
        raise PlatformConfigError(f"ip_pool.first {first} is after ip_pool.last {last}")
    for label, addr in (("first", first), ("last", last)):
        if addr not in net:
            raise PlatformConfigError(f"ip_pool.{label} {addr} is outside {net}")
    for raw in pool.get("reserved", []):
        if not first <= ipaddress.IPv4Address(raw) <= last:
            raise PlatformConfigError(f"ip_pool.reserved {raw} is outside the pool range")


def _describe(err: Any) -> str:
    path = ".".join(str(p) for p in err.absolute_path)
    return f"{path}: {err.message}" if path else str(err.message)


def load_platform(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text())
    if not isinstance(cfg, dict):
        raise PlatformConfigError(f"{path} does not contain a mapping")
    validate_platform(cfg)
    return cfg


def pool_addresses(cfg: dict[str, Any]) -> list[str]:
    """Every address the IPAM should own: the configured range minus reserved ones."""
    pool = cfg["ip_pool"]
    first = int(ipaddress.IPv4Address(pool["first"]))
    last = int(ipaddress.IPv4Address(pool["last"]))
    reserved = set(pool.get("reserved", []))
    return [
        str(ipaddress.IPv4Address(i)) for i in range(first, last + 1)
        if str(ipaddress.IPv4Address(i)) not in reserved
    ]

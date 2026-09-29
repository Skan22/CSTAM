from pathlib import Path

import pytest

from ipo.config import PlatformConfigError, load_platform, pool_addresses

ROOT = Path(__file__).resolve().parents[2]


def test_repository_platform_yaml_is_valid() -> None:
    cfg = load_platform(ROOT / "platform.yaml")
    assert cfg["domain"] == "cstam.felcloud.tn"


def test_pool_addresses_covers_range_inclusive() -> None:
    cfg = load_platform(ROOT / "platform.yaml")
    addrs = pool_addresses(cfg)
    assert addrs[0] == "10.20.0.10"
    assert addrs[-1] == "10.20.0.109"
    assert len(addrs) == 100


def test_pool_addresses_skips_reserved() -> None:
    cfg = load_platform(ROOT / "platform.yaml")
    cfg["ip_pool"]["reserved"] = ["10.20.0.11", "10.20.0.12"]
    addrs = pool_addresses(cfg)
    assert "10.20.0.11" not in addrs and "10.20.0.12" not in addrs
    assert len(addrs) == 98


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "platform.yaml"
    p.write_text((ROOT / "platform.yaml").read_text() + "surprise: 1\n")
    with pytest.raises(PlatformConfigError, match="surprise"):
        load_platform(p)


def test_missing_section_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "platform.yaml"
    p.write_text("domain: cstam.felcloud.tn\n")
    with pytest.raises(PlatformConfigError):
        load_platform(p)


def test_range_outside_cidr_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "platform.yaml"
    text = (ROOT / "platform.yaml").read_text()
    p.write_text(text.replace("last: 10.20.0.109", "last: 10.20.1.5"))
    with pytest.raises(PlatformConfigError, match="outside"):
        load_platform(p)


def test_first_after_last_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "platform.yaml"
    p.write_text(
        (ROOT / "platform.yaml").read_text().replace("first: 10.20.0.10", "first: 10.20.0.200")
    )
    with pytest.raises(PlatformConfigError, match="first"):
        load_platform(p)

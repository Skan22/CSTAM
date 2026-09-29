"""Typed view over platform.yaml for the parts the control plane reads at runtime."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Settings:
    domain: str = "cstam.felcloud.tn"
    pool_target: int = 5
    max_parallel_boots: int = 3
    reserve_free_ips: int = 5
    lease_ttl_seconds: int = 7200
    quarantine_seconds: float = 60
    sandbox_image: str = "ipo-sandbox-v1"
    sandbox_flavor: str = "m1.tiny"
    backend_port: int = 80

    @classmethod
    def from_platform(cls, cfg: dict[str, Any]) -> "Settings":
        return cls(
            domain=cfg["domain"],
            pool_target=cfg["pool"]["target_size"],
            max_parallel_boots=cfg["pool"]["max_parallel_boots"],
            reserve_free_ips=cfg["pool"]["reserve_free_ips"],
            lease_ttl_seconds=cfg["lease"]["ttl_seconds"],
            quarantine_seconds=cfg["lease"]["quarantine_seconds"],
            sandbox_image=cfg["images"]["sandbox"],
            sandbox_flavor=cfg["flavors"]["sandbox"],
        )

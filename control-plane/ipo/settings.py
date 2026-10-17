"""Typed view over platform.yaml for the parts the control plane reads at runtime."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Settings:
    domain: str = "cstam.felcloud.tn"
    pool_target: int = 5
    max_parallel_boots: int = 3
    reserve_free_ips: int = 5
    pool_max: int = 40  # the warm pool grows with recent demand, up to this
    demand_window_seconds: int = 900
    lease_ttl_seconds: int = 7200
    quarantine_seconds: float = 60
    sandbox_image: str = "ipo-sandbox-v1"
    sandbox_flavor: str = "m1.tiny"
    backend_port: int = 80
    route_middlewares: tuple[str, ...] = ("rate-limit",)  # on every team's route
    gateways: tuple[str, ...] = ("gw-a", "gw-b")
    debounce_seconds: float = 0.03  # first route change waits this long for company
    heartbeat_fresh_seconds: float = 15  # three missed beats at the agent's default of 5 s

    @classmethod
    def from_platform(cls, cfg: dict[str, Any]) -> "Settings":
        return cls(
            domain=cfg["domain"],
            pool_target=cfg["pool"]["target_size"],
            max_parallel_boots=cfg["pool"]["max_parallel_boots"],
            reserve_free_ips=cfg["pool"]["reserve_free_ips"],
            pool_max=cfg["pool"].get("max_size", 40),
            demand_window_seconds=cfg["pool"].get("demand_window_seconds", 900),
            lease_ttl_seconds=cfg["lease"]["ttl_seconds"],
            quarantine_seconds=cfg["lease"]["quarantine_seconds"],
            sandbox_image=cfg["images"]["sandbox"],
            sandbox_flavor=cfg["flavors"]["sandbox"],
        )

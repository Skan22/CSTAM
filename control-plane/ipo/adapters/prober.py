"""Probes that a team's hostname answers through the gateway VIP."""

import time

import httpx


class NullProber:
    """For development without a VIP: nothing to probe."""

    def probe(self, host: str, timeout: float) -> None:
        return None


class HttpProber:
    """Requests `host` through the VIP until the sandbox answers, or `timeout` runs out."""

    def __init__(self, vip: str, *, port: int = 80, scheme: str = "http",
                 interval: float = 0.5) -> None:
        self.base = f"{scheme}://{vip}:{port}/"
        self.interval = interval

    def probe(self, host: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last = "no attempt"
        while True:
            try:
                r = httpx.get(self.base, headers={"Host": host}, timeout=min(2.0, timeout),
                              follow_redirects=False)
                if r.status_code < 500:
                    return
                last = f"HTTP {r.status_code}"
            except httpx.HTTPError as exc:
                last = str(exc) or type(exc).__name__
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{host} did not answer through {self.base}: {last}")
            time.sleep(self.interval)

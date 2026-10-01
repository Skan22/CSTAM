"""The container health check: exits 0 when this replica's /readyz answers 200, 1 otherwise.

Run as `python -m ipo.healthcheck` so the Quadlet's HealthCmd has nothing to quote.
"""

import os
import sys
import urllib.request
from collections.abc import Mapping


def check(env: Mapping[str, str]) -> int:
    port = env.get("IPO_BIND", "0.0.0.0:8000").rpartition(":")[2]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=3) as r:
            return 0 if r.status == 200 else 1
    except OSError:  # refused, timed out, or an HTTP error status
        return 1


if __name__ == "__main__":
    sys.exit(check(os.environ))

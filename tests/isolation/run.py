"""Runs the isolation suite in the namespace lab: `uv run python -m isolation.run [pytest args]`.

Needs no root: pytest runs inside a fresh user, network and mount namespace (see lab.netns).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from lab import netns, tools


def main(argv: list[str]) -> int:
    if not tools.can_unshare():
        print("unprivileged user namespaces are not available here", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="ipo-isolation-", dir=os.environ.get("IPO_LAB_TMP")))
    env = {**os.environ, "IPO_ISOLATION_LAB": "1", "IPO_LAB_WORKDIR": str(work)}
    cmd = netns.in_lab([sys.executable, "-m", "pytest", "isolation", "-p", "no:cacheprovider",
                        *(argv or ["-v"])])
    try:
        return subprocess.run(cmd, env=env, cwd=Path(__file__).parents[1]).returncode
    finally:
        print(f"lab logs: {work}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

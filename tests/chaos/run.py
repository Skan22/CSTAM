"""Runs the chaos suite: `uv run python -m chaos.run [pytest args]`.

Starts Postgres here (it refuses to run as root) and then pytest inside a fresh user, network and
mount namespace, where the lab may create bridges, namespaces and addresses without privileges.
"""

import os
import sys
import tempfile
from pathlib import Path

from lab import netns, tools
from lab.stack import Postgres


def main(argv: list[str]) -> int:
    if not tools.can_unshare():
        print("unprivileged user namespaces are not available here", file=sys.stderr)
        return 2
    import subprocess
    work = Path(tempfile.mkdtemp(prefix="ipo-chaos-", dir=os.environ.get("IPO_LAB_TMP")))
    pg = Postgres(work / "pg")
    pg.start()
    try:
        env = {**os.environ, "IPO_LAB_DSN": pg.dsn, "IPO_LAB_WORKDIR": str(work / "lab")}
        cmd = netns.in_lab([sys.executable, "-m", "pytest", "chaos", "-p", "no:cacheprovider",
                            *(argv or ["-v"])])
        return subprocess.run(cmd, env=env, cwd=Path(__file__).parents[1]).returncode
    finally:
        pg.stop()
        print(f"lab logs: {work}/lab", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Runs the load suite: `uv run python -m load.run [pytest args]`.

Same lab as the chaos suite (real keepalived, Traefik and ipo-agent behind the real control plane
with a fake cloud), in a fresh user, network and mount namespace. Results go to
`load-report.jsonl` in the run's directory, which is printed at the end.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from lab import netns, tools
from lab.stack import Postgres


def main(argv: list[str]) -> int:
    if not tools.can_unshare():
        print("unprivileged user namespaces are not available here", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="ipo-load-", dir=os.environ.get("IPO_LAB_TMP")))
    pg = Postgres(work / "pg")
    pg.start()
    try:
        env = {**os.environ, "IPO_LAB_DSN": pg.dsn, "IPO_LAB_WORKDIR": str(work / "lab")}
        cmd = netns.in_lab([sys.executable, "-m", "pytest", "load", "-p", "no:cacheprovider",
                            "-s", *(argv or ["-v"])])
        return subprocess.run(cmd, env=env, cwd=Path(__file__).parents[1]).returncode
    finally:
        pg.stop()
        print(f"lab logs and load-report.jsonl: {work}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

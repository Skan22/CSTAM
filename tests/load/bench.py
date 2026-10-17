"""The whole benchmark: `uv run python -m load.bench [OUT_DIR]` (about 20 minutes).

Runs the load suite (provisioning, bursts of 10/20/30 five times each, rollback, expiry and
recycling, throughput, control-plane crash) and the chaos suite (failover ten times per failure),
then prints the report tables. Everything lands in OUT_DIR (default: a fresh temp directory).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parents[1]


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path(tempfile.mkdtemp(prefix="ipo-bench-"))
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "IPO_LAB_TMP": str(out), "IPO_LOAD_ROUNDS": "5", "IPO_CHAOS_RUNS": "10",
           "IPO_LIFECYCLE_TEAMS": "20"}
    for module in ("load.run", "chaos.run"):
        print(f"== {module}", file=sys.stderr)
        code = subprocess.run([sys.executable, "-m", module], env=env, cwd=HERE).returncode
        if code:
            return code
    report = subprocess.run([sys.executable, "-m", "load.report", str(out)], env=env, cwd=HERE,
                            capture_output=True, text=True, check=True).stdout
    (out / "benchmark-tables.md").write_text(report)
    print(report)
    print(f"raw measurements and logs: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

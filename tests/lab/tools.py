"""Locates the real binaries the lab runs; tests skip when one is missing."""

import functools
import os
import shutil
import subprocess
from pathlib import Path

CACHE = Path(os.environ.get("IPO_LAB_CACHE", Path.home() / ".cache" / "ipo-lab"))
REPO = Path(__file__).resolve().parents[2]


def _first(env: str, *candidates: Path | str | None) -> Path | None:
    if os.environ.get(env):
        return Path(os.environ[env])
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return Path(c)
    return None


def traefik() -> Path | None:
    return _first("IPO_TRAEFIK_BIN", CACHE / "traefik", shutil.which("traefik"))


def keepalived() -> Path | None:
    built = sorted(CACHE.glob("keepalived-*/bin/keepalived"))
    return _first("IPO_KEEPALIVED_BIN", *built[-1:], shutil.which("keepalived"))


def setgroups_shim() -> Path | None:
    """An LD_PRELOAD library making setgroups() succeed, built once with cc (see nosetgroups.c)."""
    out = CACHE / "nosetgroups.so"
    src = Path(__file__).with_name("nosetgroups.c")
    if out.is_file() and out.stat().st_mtime >= src.stat().st_mtime:
        return out
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        return None
    CACHE.mkdir(parents=True, exist_ok=True)
    subprocess.run([cc, "-shared", "-fPIC", "-o", str(out), str(src)], check=True)
    return out


@functools.cache
def agent() -> Path | None:
    """The gateway agent: IPO_AGENT_BIN, or built from source into the cache."""
    if os.environ.get("IPO_AGENT_BIN"):
        return Path(os.environ["IPO_AGENT_BIN"])
    if not shutil.which("go"):
        return None
    out = CACHE / "ipo-agent"
    CACHE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["go", "build", "-o", str(out), "./cmd/ipo-agent"],
                   cwd=REPO / "gateway-agent", check=True)
    return out


def can_unshare() -> bool:
    """True if unprivileged user, network and mount namespaces work here."""
    try:
        return subprocess.run(["unshare", "-Urnm", "true"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def missing() -> list[str]:
    gone = [n for n, p in (("traefik", traefik()), ("keepalived", keepalived())) if p is None]
    if not can_unshare():
        gone.append("unprivileged user+network namespaces")
    for tool in ("ip", "nft", "curl", "go"):
        if not shutil.which(tool):
            gone.append(tool)
    return gone

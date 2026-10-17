"""Provisioning, teardown and address recycling on the real cloud.

    cd control-plane && uv run python -m bench.real_cloud --clouds ~/clouds.yaml --out real.jsonl

Needs the platform's networks and security groups to exist (`pulumi up`; `python -m chaos.cloud up`
in tests/ builds them). Runs the real control plane in this process against the real OpenStack
API, a throwaway Postgres, in-process stand-ins for the two gateways' acks and no VIP probe: what
is measured is the cloud (Nova boots, Neutron ports) and the control plane, not Traefik, which the
lab benchmark measures. Sandbox VMs are stock Debian 13, not the baked image.

Leaves the cloud as it found it: every resource tagged `ipo` is deleted at the end, even after a
failure, and the script says so.
"""

import argparse
import json
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from ipo.adapters.local_agent import LocalAgent
from ipo.adapters.openstack.sdk import OpenStackCloud
from ipo.db.migrate import upgrade
from ipo.domain.signing import Signer
from ipo.main import Runtime, build_runtime
from ipo.settings import Settings

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests"))
from load.burst import Burst, Registrar  # type: ignore[import-not-found]  # shared with the lab

IMAGE = "Debian 13 - Trixie"
ADMIN = ("bench@example.com", "bench-password-1234")


def pg_bin(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for d in ("/usr/bin", "/usr/lib/postgresql/16/bin", "/usr/lib/postgresql/17/bin"):
        if (Path(d) / name).exists():
            return str(Path(d) / name)
    raise RuntimeError(f"{name} not found")


@contextmanager
def postgres() -> Iterator[str]:
    root = Path(tempfile.mkdtemp(prefix="ipo-realbench-"))
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    subprocess.run([pg_bin("initdb"), "-D", str(data), "-U", "ipo", "--auth=trust", "-E", "UTF8"],
                   check=True, capture_output=True)
    opts = (f"-c listen_addresses='' -c unix_socket_directories={sock} -c fsync=off "
            "-c max_connections=200")
    subprocess.run([pg_bin("pg_ctl"), "-D", str(data), "-o", opts, "-w", "-l", str(root / "log"),
                    "start"], check=True, capture_output=True)
    subprocess.run([pg_bin("createdb"), "-h", str(sock), "-U", "ipo", "ipo"], check=True)
    try:
        yield f"postgresql://ipo@/ipo?host={sock}"
    finally:
        subprocess.run([pg_bin("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                       capture_output=True)
        shutil.rmtree(root, ignore_errors=True)


class TimedCloud:
    """Wraps the real adapter and records how long each OpenStack operation takes."""

    def __init__(self, inner: OpenStackCloud) -> None:
        self._inner = inner
        self.timings: dict[str, list[float]] = {}

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self._inner, name)
        if not callable(fn):
            return fn

        def timed(*a: Any, **kw: Any) -> Any:
            t0 = time.monotonic()
            try:
                return fn(*a, **kw)
            finally:
                self.timings.setdefault(name, []).append(time.monotonic() - t0)
        return timed

    def take(self, name: str) -> list[float]:
        return self.timings.pop(name, [])


def stats(xs: list[float]) -> dict[str, float]:
    xs = sorted(xs)
    if not xs:
        return {}
    return {"n": len(xs), "min": round(xs[0], 2), "p50": round(statistics.median(xs), 2),
            "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 2), "max": round(xs[-1], 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clouds", type=Path, required=True)
    ap.add_argument("--cloud", default="")
    ap.add_argument("--count", type=int, default=10, help="teams per burst")
    ap.add_argument("--cold-rounds", type=int, default=2, help="cold bursts to run")
    ap.add_argument("--cold-count", type=int, default=10, help="teams in each cold burst")
    ap.add_argument("--out", type=Path, default=Path("real.jsonl"))
    args = ap.parse_args()

    clouds = yaml.safe_load(args.clouds.expanduser().read_text())["clouds"]
    cloud_name = args.cloud or next(iter(clouds))
    platform = yaml.safe_load((REPO / "platform.yaml").read_text())
    platform["images"]["sandbox"] = IMAGE
    platform["pool"].update(target_size=0, reserve_free_ips=0, max_parallel_boots=10,
                              demand_window_seconds=0)  # the pool follows only what the script sets
    platform["lease"]["quarantine_seconds"] = 5

    lines: list[dict[str, Any]] = []

    def emit(name: str, **data: Any) -> None:
        line = {"scenario": name, **data}
        lines.append(line)
        print(json.dumps(line), flush=True)

    with postgres() as dsn, tempfile.TemporaryDirectory() as tmp:
        plat_file = Path(tmp) / "platform.yaml"
        plat_file.write_text(yaml.safe_dump(platform))
        env = {
            "IPO_DATABASE_URL": dsn, "IPO_PLATFORM": str(plat_file), "IPO_JWT_SECRET": secrets.token_hex(24),
            "IPO_CLOUD": "openstack", "OS_CLIENT_CONFIG_FILE": str(args.clouds.expanduser()),
            "OS_CLOUD": cloud_name, "IPO_OS_NETWORK": "sandbox-net",
            "IPO_OS_SECURITY_GROUPS": "sg-sandbox.sandbox", "IPO_ADMIN_EMAIL": ADMIN[0],
            "IPO_ADMIN_PASSWORD": ADMIN[1], "IPO_WORKERS": "8",
        }
        upgrade(dsn)
        import os
        os.environ.update({k: v for k, v in env.items() if k.startswith("OS_")})
        base = Settings.from_platform(platform)
        real = OpenStackCloud.from_env(env, base)
        cloud = TimedCloud(real)
        signer = Signer.generate()
        agents = {g: LocalAgent(signer.public_b64) for g in base.gateways}
        rt: Runtime = build_runtime(env, cloud=cloud, signer=signer, agents=agents)
        client = TestClient(rt.app)
        token = client.post("/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).json()
        client.headers["Authorization"] = f"Bearer {token['access_token']}"

        def api(method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
                ) -> tuple[int, Any]:
            r = client.request(method, path, json=body, headers=headers)
            return r.status_code, (r.json() if r.content else None)

        reg = Registrar(api, None, base.domain, timeout=300, poll=0.5)

        def clean() -> None:
            for t in api("GET", "/v1/teams")[1]:
                api("DELETE", f"/v1/teams/{t['id']}")
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if not [t for t in api("GET", "/v1/teams")[1]]:
                    return
                time.sleep(1)

        def settle_pool(n: int) -> float:
            api("PATCH", "/v1/settings", {"pool_target": n})
            t0 = time.monotonic()
            while api("GET", "/v1/pool")[1]["size"] < n:
                if time.monotonic() - t0 > 600:
                    raise TimeoutError(f"pool did not reach {n}")
                time.sleep(1)
            return time.monotonic() - t0

        rt.start()
        try:
            n = args.count
            # 1. filling the warm pool: n VMs booted concurrently by the pool manager
            took = settle_pool(n)
            emit("real-pool-fill", vms=n, seconds=round(took, 1), parallel_boots=10,
                 nova_boot=stats(cloud.take("ensure_server")), neutron_port=stats(cloud.take("ensure_port")))
            # 2. n simultaneous registrations, all claiming warm VMs
            warm: Burst = reg.run(n, "warm")
            emit("real-burst-warm", **warm.summary(), personalize=stats(cloud.take("set_server_metadata")))
            assert not warm.failures, warm.summary()
            t0 = time.monotonic()
            clean()
            emit("real-teardown", teams=n, seconds=round(time.monotonic() - t0, 1),
                 delete_server=stats(cloud.take("delete_server")), delete_port=stats(cloud.take("delete_port")))
            # 3. n simultaneous registrations with no warm VM: each boots its own. The pool the
            # warm burst emptied has refilled by now; let it, then discard it, so the cold boots
            # compete with nothing (twenty concurrent boots are slower than ten).
            settle_pool(n)
            api("PATCH", "/v1/settings", {"pool_target": 0})
            while (c := api("GET", "/v1/ipam")[1]["counts"]).get("pooled", 0) or c.get("draining", 0):
                time.sleep(1)
            for rnd in range(args.cold_rounds):
                cloud.take("ensure_server")
                cold: Burst = reg.run(args.cold_count, f"cold{rnd}")
                emit("real-burst-cold", round=rnd, **cold.summary(),
                     nova_boot=stats(cloud.take("ensure_server")))
                assert not cold.failures, cold.summary()
                # 4. teardown, then the time until the addresses are usable again
                ips = {r.ip for r in cold.results}
                t0 = time.monotonic()
                clean()
                torn = time.monotonic() - t0
                freed: dict[str, float] = {}
                while len(freed) < len(ips):
                    for lease in api("GET", "/v1/ipam")[1]["leases"]:
                        if lease["ip"] in ips and lease["state"] == "free":
                            freed.setdefault(lease["ip"], time.monotonic() - t0)
                    if time.monotonic() - t0 > 300:
                        break
                    time.sleep(0.5)
                emit("real-recycle", round=rnd, teams=args.cold_count, quarantine_s=5, teardown_s=round(torn, 1),
                     address_free=stats(list(freed.values())),
                     delete_server=stats(cloud.take("delete_server")),
                     delete_port=stats(cloud.take("delete_port")))
        finally:
            rt.shutdown()
            left_s, left_p = real.list_servers(), real.list_ports()
            for s in left_s:
                real.delete_server(s.id)
            for p in left_p:
                real.delete_port(p.id)
            emit("real-cleanup", left_servers=len(left_s), left_ports=len(left_p))
    args.out.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

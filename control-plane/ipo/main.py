"""Runs the API, the saga workers and every controller in one process.

Replicas are safe: workers coordinate through row locks and each controller through a leader
advisory lock, so `IPO_WORKERS` and the number of replicas can grow independently.

Configuration comes from the environment:

    IPO_DATABASE_URL   postgresql://... (required)
    IPO_PLATFORM       path to platform.yaml (required)
    IPO_JWT_SECRET     at least 32 bytes (required)
    IPO_CLOUD          `openstack` (default) or `fake`
    IPO_SIGNING_KEY    base64 Ed25519 seed; generated (and warned about) if unset
    IPO_AGENTS         gw-a=https://10.0.0.11:8443,gw-b=https://10.0.0.12:8443
    IPO_AGENT_CERT / IPO_AGENT_KEY / IPO_AGENT_CA   mTLS material for the agents
    IPO_VIP            the gateway VIP, for the registration probe
    IPO_WORKERS        saga worker threads (default 2)
    IPO_BIND           host:port for the API (default 0.0.0.0:8000)
    IPO_MIGRATE        `1` to run migrations at start
    IPO_ADMIN_EMAIL / IPO_ADMIN_PASSWORD   creates or resets this admin at start
"""

import logging
import os
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import psycopg
import uvicorn
from fastapi import FastAPI

from ipo import runtime_settings
from ipo.adapters.local_agent import LocalAgent
from ipo.adapters.openstack.base import Cloud
from ipo.adapters.openstack.fake import FakeCloud
from ipo.adapters.prober import HttpProber, NullProber
from ipo.api.app import AppDeps, create_app
from ipo.api.users import create_user
from ipo.config import load_platform, pool_addresses
from ipo.controllers.compiler import (
    Agent,
    CompilerController,
    DbGateways,
    HttpAgent,
    Pusher,
)
from ipo.controllers.pool import PoolManager
from ipo.controllers.reaper import Reaper
from ipo.controllers.reconciler import Reconciler
from ipo.controllers.register_saga import Deps, Prober
from ipo.controllers.workers import build_runner, run_leader, run_worker
from ipo.db.migrate import upgrade
from ipo.domain import leases
from ipo.domain.signing import Signer
from ipo.settings import Settings

log = logging.getLogger("ipo")


@dataclass
class Runtime:
    app: FastAPI
    settings: Settings
    connect: Callable[[], psycopg.Connection]
    agents: Mapping[str, Agent]
    stop: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout)


def _agents(env: Mapping[str, str]) -> dict[str, Agent]:
    cert = (env["IPO_AGENT_CERT"], env["IPO_AGENT_KEY"]) if env.get("IPO_AGENT_CERT") else None
    out: dict[str, Agent] = {}
    for pair in filter(None, env.get("IPO_AGENTS", "").split(",")):
        name, _, url = pair.partition("=")
        out[name.strip()] = HttpAgent(url.strip(), cert=cert, ca=env.get("IPO_AGENT_CA"))
    return out


def build_runtime(
    env: Mapping[str, str], *, cloud: Cloud | None = None,
    agents: Mapping[str, Agent] | None = None, prober: Prober | None = None,
    signer: Signer | None = None,
) -> Runtime:
    """Wire everything from `env`; the keyword arguments replace parts for tests."""
    dsn = env["IPO_DATABASE_URL"]
    cfg = load_platform(Path(env["IPO_PLATFORM"]))
    base = Settings.from_platform(cfg)
    if env.get("IPO_MIGRATE") == "1":
        upgrade(dsn)

    def connect() -> psycopg.Connection:
        return psycopg.connect(dsn, autocommit=True)

    with connect() as conn:
        leases.seed(conn, pool_addresses(cfg))
        if env.get("IPO_ADMIN_EMAIL") and env.get("IPO_ADMIN_PASSWORD"):
            create_user(conn, env["IPO_ADMIN_EMAIL"], env["IPO_ADMIN_PASSWORD"], "admin")

    if signer is None:
        if env.get("IPO_SIGNING_KEY"):
            signer = Signer.from_private_b64(env["IPO_SIGNING_KEY"])
        else:
            signer = Signer.generate()
            log.warning("IPO_SIGNING_KEY is unset: generated a key that agents will not trust "
                        "after a restart. Set it in production. Public key: %s",
                        signer.public_b64)
    fake = env.get("IPO_CLOUD") == "fake"
    if cloud is None:
        if fake:
            cloud = FakeCloud()
        else:
            from ipo.adapters.openstack.sdk import OpenStackCloud
            cloud = OpenStackCloud.from_env(env, base)
    if agents is None:
        agents = ({g: LocalAgent(signer.public_b64) for g in base.gateways}
                  if fake else _agents(env))
    if prober is None:
        prober = HttpProber(env["IPO_VIP"]) if env.get("IPO_VIP") else NullProber()

    def failover(name: str) -> None:
        fault = getattr(agents[name], "fault", None)
        if fault is None:
            raise ConnectionError(f"agent {name} cannot be faulted")
        fault()

    pusher = Pusher(agents, connect)
    deps = Deps(cloud, DbGateways(base), prober, base)
    app = create_app(AppDeps(connect, base, jwt_secret=env["IPO_JWT_SECRET"],
                             failover=failover if agents else None))
    rt = Runtime(app, base, connect, agents)

    def live() -> Settings:
        with connect() as conn:
            return runtime_settings.effective(conn, base)

    pool = PoolManager(cloud, base, connect)
    compiler = CompilerController(connect, signer, base, push=pusher.push)
    reconciler = Reconciler(cloud, base, connect, repush=pusher.push)
    reaper = Reaper(connect)
    stop = rt.stop

    def thread(name: str, target: Callable[[], object]) -> None:
        rt.threads.append(threading.Thread(target=target, name=name, daemon=True))

    for i in range(int(env.get("IPO_WORKERS", "2"))):
        runner = build_runner(connect, deps, worker_id=f"{socket.gethostname()}-{os.getpid()}-{i}")
        thread(f"worker-{i}", partial(run_worker, runner, stop))
    thread("pool", lambda: run_leader(
        connect, "pool", stop, 5.0, lambda: pool.reconcile(target=live().pool_target)))
    thread("compiler", lambda: compiler.run(stop))
    thread("reaper", lambda: reaper.run(stop))
    thread("reconciler", lambda: reconciler.run(stop))
    return rt


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    rt = build_runtime(os.environ)
    rt.start()
    host, _, port = os.environ.get("IPO_BIND", "0.0.0.0:8000").rpartition(":")  # noqa: S104
    try:
        uvicorn.run(rt.app, host=host, port=int(port), log_config=None)
    finally:
        rt.shutdown()


if __name__ == "__main__":
    main()

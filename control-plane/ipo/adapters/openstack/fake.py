"""In-memory Cloud for unit and saga tests, with fault injection."""

import itertools
import json
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path

from ipo.adapters.openstack.base import (
    TAG,
    CloudError,
    Port,
    QuotaExceeded,
    Server,
    port_name,
    server_name,
)


class FakeCloud:
    def __init__(
        self, *, max_servers: int | None = None, clock: Callable[[], float] = time.time,
        boot_seconds: float = 0.0, state_file: str | os.PathLike[str] | None = None,
    ) -> None:
        self.clock = clock
        self.boot_seconds = boot_seconds  # how long creating a server takes, as Nova's boot does
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self.ports: dict[str, Port] = {}
        self.servers: dict[str, Server] = {}
        self.max_servers = max_servers
        self.calls: dict[str, int] = defaultdict(int)
        self._faults: dict[str, list[Exception]] = defaultdict(list)
        # With a state file the "cloud" survives a restart of the process, as a real one does.
        self._state = Path(state_file) if state_file else None
        if self._state and self._state.exists():
            data = json.loads(self._state.read_text())
            self.ports = {p["id"]: Port(**{**p, "tags": tuple(p["tags"])}) for p in data["ports"]}
            self.servers = {s["id"]: Server(**{**s, "tags": tuple(s["tags"])})
                            for s in data["servers"]}
            self._ids = itertools.count(data["next_id"])

    def adopt(self, *, ip: str, port_id: str | None, server_id: str | None) -> None:
        """Take over a port and server a previous process created, as recorded in the database.
        The fake keeps its servers in memory; without this, every restart forgot the warm pool
        while the leases still named it (found on the first real deploy, where the first team
        failed with a KeyError for its pooled server)."""
        with self._lock:
            if port_id and port_id not in self.ports:
                self.ports[port_id] = Port(id=port_id, name=port_name(ip), ip=ip,
                                           created_at=self.clock())
            if server_id and server_id not in self.servers:
                self.servers[server_id] = Server(id=server_id, name=server_name(ip),
                                                 port_id=port_id or "", created_at=self.clock())
            taken = [int(i.rsplit("-", 1)[1]) for i in (*self.ports, *self.servers)
                     if i.rsplit("-", 1)[-1].isdigit()]
            self._ids = itertools.count(max(taken, default=0) + 1)

    def _save(self) -> None:
        if self._state is None:
            return
        next_id = next(self._ids)
        self._ids = itertools.count(next_id)
        data = {"ports": [asdict(p) for p in self.ports.values()],
                "servers": [asdict(s) for s in self.servers.values()], "next_id": next_id}
        tmp = self._state.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._state)

    # -- test controls
    def inject(self, method: str, error: Exception | None = None, times: int = 1) -> None:
        """Make the next `times` calls to `method` raise."""
        self._faults[method].extend([error or CloudError(f"injected {method} failure")] * times)

    def _enter(self, method: str) -> None:
        self.calls[method] += 1
        if self._faults[method]:
            raise self._faults[method].pop(0)

    # -- Cloud
    def ensure_port(self, *, name: str, ip: str, tags: Sequence[str] = (TAG,)) -> Port:
        with self._lock:
            self._enter("ensure_port")
            for p in self.ports.values():
                if p.name == name:
                    return p
            port = Port(id=f"port-{next(self._ids)}", name=name, ip=ip, tags=tuple(tags),
                        created_at=self.clock())
            self.ports[port.id] = port
            self._save()
            return port

    def ensure_server(
        self, *, name: str, port_id: str, tags: Sequence[str] = (TAG,),
        metadata: Mapping[str, str] | None = None,
    ) -> Server:
        with self._lock:
            self._enter("ensure_server")
            for s in self.servers.values():
                if s.name == name:
                    return s
            if self.max_servers is not None and len(self.servers) >= self.max_servers:
                raise QuotaExceeded("instances quota exceeded")
            server = Server(id=f"srv-{next(self._ids)}", name=name, port_id=port_id,
                            tags=tuple(tags), metadata=dict(metadata or {}),
                            created_at=self.clock())
            self.servers[server.id] = server
            self._save()
        if self.boot_seconds:
            time.sleep(self.boot_seconds)  # outside the lock: boots overlap, like Nova's
        return server

    def set_server_metadata(self, server_id: str, metadata: Mapping[str, str]) -> None:
        with self._lock:
            self._enter("set_server_metadata")
            s = self.servers[server_id]
            self.servers[server_id] = replace(s, metadata={**s.metadata, **metadata})
            self._save()

    def clear_server_metadata(self, server_id: str, keys: Sequence[str]) -> None:
        with self._lock:
            self._enter("clear_server_metadata")
            s = self.servers.get(server_id)
            if s:
                kept = {k: v for k, v in s.metadata.items() if k not in keys}
                self.servers[server_id] = replace(s, metadata=kept)
                self._save()

    def delete_server(self, server_id: str) -> None:
        with self._lock:
            self._enter("delete_server")
            self.servers.pop(server_id, None)
            self._save()

    def delete_port(self, port_id: str) -> None:
        with self._lock:
            self._enter("delete_port")
            self.ports.pop(port_id, None)
            self._save()

    def list_ports(self, tag: str = TAG) -> list[Port]:
        with self._lock:
            self._enter("list_ports")
            return [p for p in self.ports.values() if tag in p.tags]

    def list_servers(self, tag: str = TAG) -> list[Server]:
        with self._lock:
            self._enter("list_servers")
            return [s for s in self.servers.values() if tag in s.tags]

"""In-memory Cloud for unit and saga tests, with fault injection."""

import itertools
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from ipo.adapters.openstack.base import TAG, CloudError, Port, QuotaExceeded, Server


class FakeCloud:
    def __init__(
        self, *, max_servers: int | None = None, clock: Callable[[], float] = time.time
    ) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self.ports: dict[str, Port] = {}
        self.servers: dict[str, Server] = {}
        self.max_servers = max_servers
        self.calls: dict[str, int] = defaultdict(int)
        self._faults: dict[str, list[Exception]] = defaultdict(list)

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
            return server

    def set_server_metadata(self, server_id: str, metadata: Mapping[str, str]) -> None:
        with self._lock:
            self._enter("set_server_metadata")
            s = self.servers[server_id]
            self.servers[server_id] = replace(s, metadata={**s.metadata, **metadata})

    def clear_server_metadata(self, server_id: str, keys: Sequence[str]) -> None:
        with self._lock:
            self._enter("clear_server_metadata")
            s = self.servers.get(server_id)
            if s:
                kept = {k: v for k, v in s.metadata.items() if k not in keys}
                self.servers[server_id] = replace(s, metadata=kept)

    def delete_server(self, server_id: str) -> None:
        with self._lock:
            self._enter("delete_server")
            self.servers.pop(server_id, None)

    def delete_port(self, port_id: str) -> None:
        with self._lock:
            self._enter("delete_port")
            self.ports.pop(port_id, None)

    def list_ports(self, tag: str = TAG) -> list[Port]:
        with self._lock:
            self._enter("list_ports")
            return [p for p in self.ports.values() if tag in p.tags]

    def list_servers(self, tag: str = TAG) -> list[Server]:
        with self._lock:
            self._enter("list_servers")
            return [s for s in self.servers.values() if tag in s.tags]

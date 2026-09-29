"""`Cloud` on FelCloud's OpenStack through openstacksdk.

Written against the SDK's documented surface and exercised in tests with a stub connection.
It has not yet been run against a real cloud; the M0 exercise of a single create-and-delete
cycle is where any mismatch (Nova microversion for server tags, Neutron port-security defaults)
will show up.
"""

import functools
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from openstack import exceptions as os_exc

from ipo.adapters.openstack.base import TAG, CloudError, Port, QuotaExceeded, Server
from ipo.settings import Settings


def _epoch(stamp: str | None) -> float:
    if not stamp:
        return 0.0
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _translated[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except os_exc.SDKException as exc:
            text = str(exc)
            if "quota" in text.lower():
                raise QuotaExceeded(text) from exc
            raise CloudError(text) from exc
    return wrapper


class OpenStackCloud:
    def __init__(
        self, conn: Any, *, network: str, image: str, flavor: str,
        wait_seconds: float = 180.0, key_name: str | None = None,
        security_groups: Sequence[str] = (),
    ) -> None:
        self.conn = conn
        self.network_name, self.image_name, self.flavor_name = network, image, flavor
        self.wait_seconds = wait_seconds
        self.key_name = key_name
        self.security_groups = list(security_groups)

    @classmethod
    def from_env(cls, env: Mapping[str, str], settings: Settings) -> "OpenStackCloud":
        import openstack

        return cls(
            openstack.connect(cloud=env.get("OS_CLOUD") or None),
            network=env.get("IPO_OS_NETWORK", "sandbox-net"), image=settings.sandbox_image,
            flavor=settings.sandbox_flavor, key_name=env.get("IPO_OS_KEY_NAME") or None,
            security_groups=[g for g in env.get("IPO_OS_SECURITY_GROUPS", "").split(",") if g])

    # -- lookups
    def _network_id(self) -> str:
        net = self.conn.network.find_network(self.network_name, ignore_missing=False)
        return str(net.id)

    def _port(self, p: Any) -> Port:
        ips = [f["ip_address"] for f in (p.fixed_ips or [])]
        return Port(id=p.id, name=p.name, ip=ips[0] if ips else "",
                    tags=tuple(p.tags or ()), created_at=_epoch(p.created_at))

    def _server(self, s: Any, port_by_device: Mapping[str, str]) -> Server:
        return Server(id=s.id, name=s.name, port_id=port_by_device.get(s.id, ""),
                      tags=tuple(s.tags or ()), metadata=dict(s.metadata or {}),
                      created_at=_epoch(s.created_at))

    # -- Cloud
    @_translated
    def ensure_port(self, *, name: str, ip: str, tags: Sequence[str] = (TAG,)) -> Port:
        for p in self.conn.network.ports(name=name):
            if p.name == name:
                return self._port(p)
        port = self.conn.network.create_port(
            name=name, network_id=self._network_id(), fixed_ips=[{"ip_address": ip}],
            security_group_ids=self._security_group_ids() or None)
        self.conn.network.set_tags(port, list(tags))
        return self._port(self.conn.network.get_port(port.id))

    def _security_group_ids(self) -> list[str]:
        return [self.conn.network.find_security_group(g, ignore_missing=False).id
                for g in self.security_groups]

    @_translated
    def ensure_server(
        self, *, name: str, port_id: str, tags: Sequence[str] = (TAG,),
        metadata: Mapping[str, str] | None = None,
    ) -> Server:
        existing = next((s for s in self.conn.compute.servers(name=name) if s.name == name), None)
        if existing is None:
            kwargs: dict[str, Any] = {}
            if self.key_name:
                kwargs["key_name"] = self.key_name
            existing = self.conn.compute.create_server(
                name=name, image_id=self.conn.image.find_image(
                    self.image_name, ignore_missing=False).id,
                flavor_id=self.conn.compute.find_flavor(
                    self.flavor_name, ignore_missing=False).id,
                networks=[{"port": port_id}], metadata=dict(metadata or {}), tags=list(tags),
                **kwargs)
        ready = self.conn.compute.wait_for_server(existing, status="ACTIVE",
                                                  wait=self.wait_seconds)
        return self._server(ready, {ready.id: port_id})

    @_translated
    def set_server_metadata(self, server_id: str, metadata: Mapping[str, str]) -> None:
        self.conn.compute.set_server_metadata(server_id, **metadata)

    @_translated
    def clear_server_metadata(self, server_id: str, keys: Sequence[str]) -> None:
        try:
            self.conn.compute.delete_server_metadata(server_id, list(keys))
        except os_exc.NotFoundException:
            pass

    @_translated
    def delete_server(self, server_id: str) -> None:
        self.conn.compute.delete_server(server_id, ignore_missing=True)

    @_translated
    def delete_port(self, port_id: str) -> None:
        self.conn.network.delete_port(port_id, ignore_missing=True)

    @_translated
    def list_ports(self, tag: str = TAG) -> list[Port]:
        return [self._port(p) for p in self.conn.network.ports(tags=tag)]

    @_translated
    def list_servers(self, tag: str = TAG) -> list[Server]:
        by_device = {p.device_id: p.id for p in self.conn.network.ports(tags=tag) if p.device_id}
        return [self._server(s, by_device) for s in self.conn.compute.servers(tags=tag)]

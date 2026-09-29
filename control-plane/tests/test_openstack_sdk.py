"""The SDK adapter against a stub that mimics the parts of openstacksdk it uses."""

from types import SimpleNamespace as NS
from typing import Any

import pytest
from openstack import exceptions as os_exc

from ipo.adapters.openstack.base import TAG, CloudError, QuotaExceeded, port_name, server_name
from ipo.adapters.openstack.sdk import OpenStackCloud


class Network:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.creates = 0

    def find_network(self, name: str, ignore_missing: bool = True) -> Any:
        return NS(id="net-1", name=name)

    def find_security_group(self, name: str, ignore_missing: bool = True) -> Any:
        return NS(id=f"sg-{name}")

    def ports(self, name: str | None = None, tags: str | None = None) -> list[Any]:
        return [p for p in self.store.values()
                if (name is None or p.name == name) and (tags is None or tags in p.tags)]

    def create_port(self, **kw: Any) -> Any:
        self.creates += 1
        p = NS(id=f"p{self.creates}", name=kw["name"], fixed_ips=kw["fixed_ips"], tags=[],
               device_id=None, created_at="2026-01-01T00:00:00Z")
        self.store[p.id] = p
        return p

    def set_tags(self, port: Any, tags: list[str]) -> None:
        port.tags = tags

    def get_port(self, pid: str) -> Any:
        return self.store[pid]

    def delete_port(self, pid: str, ignore_missing: bool = True) -> None:
        self.store.pop(pid, None)


class Compute:
    def __init__(self, network: Network) -> None:
        self.network = network
        self.store: dict[str, Any] = {}
        self.fail: Exception | None = None
        self.final_status = "ACTIVE"

    def servers(self, name: str | None = None, tags: str | None = None) -> list[Any]:
        return [s for s in self.store.values()
                if (name is None or s.name == name) and (tags is None or tags in s.tags)]

    def find_flavor(self, n: str, ignore_missing: bool = True) -> Any:
        return NS(id=f"flavor-{n}")

    def create_server(self, **kw: Any) -> Any:
        if self.fail:
            raise self.fail
        s = NS(id=f"s{len(self.store) + 1}", name=kw["name"], tags=kw["tags"],
               metadata=kw["metadata"], status="BUILD", created_at="2026-01-01T00:00:10Z",
               port=kw["networks"][0]["port"])
        self.store[s.id] = s
        self.network.store[s.port].device_id = s.id
        return s

    def wait_for_server(self, s: Any, status: str, wait: float) -> Any:
        if self.final_status == "ERROR":
            raise os_exc.ResourceFailure("server went to ERROR")
        s.status = status
        return s

    def set_server_metadata(self, sid: str, **md: str) -> None:
        self.store[sid].metadata.update(md)

    def delete_server_metadata(self, sid: str, keys: list[str]) -> None:
        for k in keys:
            self.store[sid].metadata.pop(k, None)

    def delete_server(self, sid: str, ignore_missing: bool = True) -> None:
        self.store.pop(sid, None)


@pytest.fixture
def cloud() -> OpenStackCloud:
    net = Network()
    conn = NS(network=net, compute=Compute(net),
              image=NS(find_image=lambda n, ignore_missing=True: NS(id=f"img-{n}")))
    return OpenStackCloud(conn, network="sandbox-net", image="ipo-sandbox-v1", flavor="m1.tiny")


def test_ensure_is_idempotent_and_tags_what_it_creates(cloud: OpenStackCloud) -> None:
    a = cloud.ensure_port(name=port_name("10.20.0.10"), ip="10.20.0.10")
    b = cloud.ensure_port(name=port_name("10.20.0.10"), ip="10.20.0.10")
    assert a == b and cloud.conn.network.creates == 1 and a.tags == (TAG,)
    assert a.ip == "10.20.0.10" and a.created_at > 0
    s1 = cloud.ensure_server(name=server_name("10.20.0.10"), port_id=a.id, metadata={"k": "v"})
    s2 = cloud.ensure_server(name=server_name("10.20.0.10"), port_id=a.id)
    assert s1.id == s2.id and len(cloud.conn.compute.store) == 1
    assert s1.metadata == {"k": "v"} and s1.tags == (TAG,)


def test_listing_links_each_server_to_its_port(cloud: OpenStackCloud) -> None:
    p = cloud.ensure_port(name="ipo-port-a", ip="10.20.0.11")
    s = cloud.ensure_server(name="ipo-vm-a", port_id=p.id)
    assert [x.id for x in cloud.list_ports()] == [p.id]
    assert [(x.id, x.port_id) for x in cloud.list_servers()] == [(s.id, p.id)]
    assert cloud.list_servers(tag="someone-elses") == []


def test_metadata_set_clear_and_deletes_are_idempotent(cloud: OpenStackCloud) -> None:
    p = cloud.ensure_port(name="ipo-port-a", ip="10.20.0.11")
    s = cloud.ensure_server(name="ipo-vm-a", port_id=p.id)
    cloud.set_server_metadata(s.id, {"ipo.team": "t1", "ipo.slug": "alpha"})
    cloud.clear_server_metadata(s.id, ["ipo.team", "ipo.slug"])
    assert cloud.list_servers()[0].metadata == {}
    for _ in range(2):
        cloud.delete_server(s.id)
        cloud.delete_port(p.id)
    assert cloud.list_servers() == [] and cloud.list_ports() == []


def test_quota_errors_are_distinguished_from_other_failures(cloud: OpenStackCloud) -> None:
    p = cloud.ensure_port(name="ipo-port-a", ip="10.20.0.11")
    compute: Compute = cloud.conn.compute
    compute.fail = os_exc.HttpException(message="Quota exceeded for instances: Requested 1")
    with pytest.raises(QuotaExceeded):
        cloud.ensure_server(name="ipo-vm-a", port_id=p.id)
    compute.fail = os_exc.HttpException(message="boom")
    with pytest.raises(CloudError) as info:
        cloud.ensure_server(name="ipo-vm-a", port_id=p.id)
    assert not isinstance(info.value, QuotaExceeded)


def test_a_server_that_errors_while_booting_is_a_cloud_error(cloud: OpenStackCloud) -> None:
    p = cloud.ensure_port(name="ipo-port-a", ip="10.20.0.11")
    cloud.conn.compute.final_status = "ERROR"
    with pytest.raises(CloudError):
        cloud.ensure_server(name="ipo-vm-a", port_id=p.id)

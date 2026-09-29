"""Release a lease's cloud resources and move it through quarantine.

Shared by pool discards, failed provisioning, saga undo and the reaper. Every step is safe to
repeat, so a crash halfway is finished by simply calling it again.
"""

import psycopg

from ipo.adapters.openstack.base import Cloud, port_name, server_name
from ipo.domain import leases


def release_resources(
    conn: psycopg.Connection, cloud: Cloud, ip: str, *, quarantine_seconds: float
) -> leases.Lease:
    lease = leases.get(conn, ip)
    if lease.state in ("free", "quarantined"):
        return lease
    if lease.state != "draining":
        lease = leases.begin_drain(conn, ip)
    # Delete by recorded id and by deterministic name: a crash between creating a resource and
    # recording it must not leave an orphan behind.
    for server in cloud.list_servers():
        if server.id == lease.server_id or server.name == server_name(ip):
            cloud.delete_server(server.id)
    for port in cloud.list_ports():
        if port.id == lease.port_id or port.name == port_name(ip):
            cloud.delete_port(port.id)
    return leases.quarantine(conn, ip, seconds=quarantine_seconds)

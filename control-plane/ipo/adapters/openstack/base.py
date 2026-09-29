"""The seam between the control plane and OpenStack.

Every mutating call is idempotent and keyed by a deterministic name, so a saga step that ran
twice (a retry, or a resume after a crash) converges on the same resources instead of leaking.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

TAG = "ipo"  # every resource we create carries this tag so the reconciler can recognise it


class CloudError(Exception):
    """A failed OpenStack call. Retrying may help."""


class QuotaExceeded(CloudError):
    """Quota is exhausted; callers should back off rather than hammer the API."""


def port_name(ip: str) -> str:
    return f"ipo-port-{ip}"


def server_name(ip: str) -> str:
    return f"ipo-vm-{ip}"


@dataclass(frozen=True)
class Port:
    id: str
    name: str
    ip: str
    tags: tuple[str, ...] = (TAG,)


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    port_id: str
    tags: tuple[str, ...] = (TAG,)
    metadata: Mapping[str, str] = field(default_factory=dict)


class Cloud(Protocol):
    def ensure_port(self, *, name: str, ip: str, tags: Sequence[str] = (TAG,)) -> Port: ...

    def ensure_server(
        self, *, name: str, port_id: str, tags: Sequence[str] = (TAG,),
        metadata: Mapping[str, str] | None = None,
    ) -> Server:
        """Create the VM if missing and return once it is ACTIVE."""
        ...

    def set_server_metadata(self, server_id: str, metadata: Mapping[str, str]) -> None: ...

    def clear_server_metadata(self, server_id: str, keys: Sequence[str]) -> None: ...

    def delete_server(self, server_id: str) -> None:
        """Idempotent: deleting a missing server is not an error."""
        ...

    def delete_port(self, port_id: str) -> None:
        """Idempotent: deleting a missing port is not an error."""
        ...

    def list_ports(self, tag: str = TAG) -> list[Port]: ...

    def list_servers(self, tag: str = TAG) -> list[Server]: ...

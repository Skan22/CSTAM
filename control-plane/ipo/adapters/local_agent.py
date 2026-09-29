"""An in-process gateway agent for development and tests.

It applies the same acceptance rules as the real agent: a valid signature, and a version newer
than the one it runs. It holds the config in memory instead of handing it to Traefik.
"""

import threading

from ipo.controllers.compiler import Ack, ConfigVersion
from ipo.domain import signing


class LocalAgent:
    def __init__(self, public_key_b64: str) -> None:
        self._pub = public_key_b64
        self._lock = threading.Lock()
        self.live_version = 0
        self.body = ""
        self.faulted = False

    def put_config(self, envelope: ConfigVersion) -> Ack:
        with self._lock:
            try:
                signing.verify(self._pub, envelope.version, envelope.sha256, envelope.body,
                               envelope.signature)
            except signing.InvalidSignature:
                return Ack(False, self.live_version, "bad signature")
            if envelope.version <= self.live_version:
                return Ack(False, self.live_version, "stale version")
            self.live_version, self.body = envelope.version, envelope.body
            return Ack(True, self.live_version)

    def fault(self) -> None:
        self.faulted = True

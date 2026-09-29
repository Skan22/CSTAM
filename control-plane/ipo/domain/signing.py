"""Ed25519 signing of config versions.

The signature covers the version number, the body's SHA-256 and the body itself, so an agent
that verifies it rejects a tampered body, a body re-labelled with another version, and a
replay of an older version under a newer number.
"""

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

__all__ = ["InvalidSignature", "Signer", "sha256_hex", "verify"]

_RAW = serialization.Encoding.Raw, serialization.PublicFormat.Raw


def sha256_hex(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def _message(version: int, sha256: str, body: str) -> bytes:
    return f"ipo-config\n{version}\n{sha256}\n".encode() + body.encode()


class Signer:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key

    @classmethod
    def generate(cls) -> "Signer":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_b64(cls, seed_b64: str) -> "Signer":
        return cls(Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64)))

    @property
    def private_b64(self) -> str:
        raw = self._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return base64.b64encode(raw).decode()

    @property
    def public_b64(self) -> str:
        return base64.b64encode(self._key.public_key().public_bytes(*_RAW)).decode()

    def sign(self, version: int, sha256: str, body: str) -> str:
        return base64.b64encode(self._key.sign(_message(version, sha256, body))).decode()


def verify(public_b64: str, version: int, sha256: str, body: str, signature: str) -> None:
    """Raise `InvalidSignature` unless `signature` is valid for exactly this version, hash
    and body, and the hash really is the body's."""
    if sha256_hex(body) != sha256:
        raise InvalidSignature("sha256 does not match the body")
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
    key.verify(base64.b64decode(signature), _message(version, sha256, body))

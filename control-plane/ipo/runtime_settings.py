"""Settings an admin can change while the platform runs. They live in the `settings` table so
every replica sees the same values; anything not overridden falls back to platform.yaml."""

import dataclasses
import json
from typing import Any

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from ipo.settings import Settings

KEY = "runtime"


class SettingsPatch(BaseModel):
    """The only settings that can change at runtime; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    pool_target: int | None = Field(None, ge=0, le=200)
    reserve_free_ips: int | None = Field(None, ge=0, le=1000)
    lease_ttl_seconds: int | None = Field(None, ge=60, le=7 * 86400)
    quarantine_seconds: float | None = Field(None, ge=0, le=86400)
    max_parallel_boots: int | None = Field(None, ge=1, le=20)


FIELDS = tuple(SettingsPatch.model_fields)


def overrides(conn: psycopg.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (KEY,)).fetchone()
    return {k: v for k, v in (row[0] if row else {}).items() if k in FIELDS}


def effective(conn: psycopg.Connection, base: Settings) -> Settings:
    return dataclasses.replace(base, **overrides(conn))


def values(settings: Settings) -> dict[str, Any]:
    return {f: getattr(settings, f) for f in FIELDS}


def update(conn: psycopg.Connection, patch: SettingsPatch, *, actor: str) -> dict[str, Any]:
    """Merge `patch` into the stored overrides and audit it; returns the changes made."""
    changes = patch.model_dump(exclude_none=True)
    with conn.transaction():
        merged = {**overrides(conn), **changes}
        conn.execute(
            "INSERT INTO settings (key, value, changed_by) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,"
            " changed_by = EXCLUDED.changed_by", (KEY, json.dumps(merged), actor))
        conn.execute(
            "INSERT INTO audit_log (actor, action, target, detail) VALUES (%s, %s, %s, %s)",
            (actor, "settings.update", KEY, json.dumps({"changes": changes})))
    return changes

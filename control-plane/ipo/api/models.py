"""Request and response models. The OpenAPI document is generated from these."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(Strict):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class RegisterRequest(Strict):
    slug: str = Field(min_length=1, max_length=63, description="Becomes <slug>.<domain>")


class Registered(BaseModel):
    job_id: UUID
    team_id: UUID
    ip: str
    path: str = Field(description="`warm` claimed a pooled VM; `cold` boots a new one")
    subdomain: str
    status_url: str


class Team(BaseModel):
    id: UUID
    slug: str
    subdomain: str
    state: str
    owner: str
    ip: str | None
    created_at: datetime
    expires_at: datetime


class Teardown(BaseModel):
    job_id: UUID
    team_id: UUID
    status_url: str


class ExtendRequest(Strict):
    seconds: int = Field(gt=0, le=86400)


class Step(BaseModel):
    step: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    result: Any | None


class Job(BaseModel):
    id: UUID
    kind: str
    state: str
    team_id: UUID | None
    attempts: int
    last_error: str | None
    trace_id: str | None
    created_at: datetime
    steps: list[Step]


class Pool(BaseModel):
    size: int
    target: int
    free: int
    leased: int
    draining: int
    quarantined: int


class IpamLease(BaseModel):
    ip: str
    state: str
    team: str | None
    expires_at: datetime | None
    quarantined_until: datetime | None


class Ipam(BaseModel):
    total: int
    counts: dict[str, int]
    leases: list[IpamLease]


class Gateway(BaseModel):
    gateway: str
    vrrp_state: str
    live_version: int | None
    last_heartbeat: datetime | None


class Gateways(BaseModel):
    gateways: list[Gateway]
    split_brain: bool = Field(
        description="True while two gateways both report MASTER after the newest promotion")


class Heartbeat(Strict):
    vrrp_state: Literal["MASTER", "BACKUP", "FAULT", "UNKNOWN"]
    live_version: int = Field(ge=0, description="0 when the agent has no config yet")


class ConfigEnvelope(BaseModel):
    version: int
    sha256: str
    signature: str
    body: str


class FailoverRequest(Strict):
    pass


class Failover(BaseModel):
    gateway: str


class ConfigVersionInfo(BaseModel):
    version: int
    sha256: str
    status: str
    created_at: datetime


class ConfigVersionDetail(ConfigVersionInfo):
    signature: str
    body: str


class ConfigVersions(BaseModel):
    versions: list[ConfigVersionInfo]
    pinned: int | None


class RollbackRequest(Strict):
    version: int | None = Field(None, description="Version to pin; null unpins")


class Rollback(BaseModel):
    pinned: int | None


class SettingsChange(BaseModel):
    at: datetime
    actor: str
    detail: dict[str, Any]


class SettingsView(BaseModel):
    values: dict[str, Any]
    history: list[SettingsChange]


class AuditEntry(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    target: str | None
    detail: dict[str, Any]


class ChainStatus(BaseModel):
    valid: bool
    first_bad_id: int | None


class Audit(BaseModel):
    chain: ChainStatus
    entries: list[AuditEntry]

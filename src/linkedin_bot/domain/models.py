"""Strict boundaries; unknown candidate facts remain unknown."""

import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(UTC)


def identifier() -> str:
    return str(uuid4())


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class CandidateFact(Contract):
    id: str = Field(default_factory=identifier)
    revision: int = Field(default=1, ge=1)
    kind: str = Field(min_length=1)
    value: str | None = None
    source_reference: str = Field(min_length=1)
    status: Literal["unconfirmed", "confirmed", "rejected"] = "unconfirmed"
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def confirmation(self) -> CandidateFact:
        if self.status == "confirmed" and (self.value is None or self.confirmed_at is None):
            raise ValueError("confirmed facts require a value and confirmation time")
        if self.status != "confirmed" and self.confirmed_at is not None:
            raise ValueError("unconfirmed facts cannot have confirmation time")
        if self.confirmed_at is not None and self.confirmed_at.utcoffset() is None:
            raise ValueError("confirmation time must be timezone aware")
        return self


class CandidateProfile(Contract):
    id: str
    revision: int = Field(ge=1)
    fact_ids: tuple[str, ...] = ()
    baseline_artifact_id: str | None = None


class SearchPreferences(Contract):
    revision: int = Field(default=1, ge=1)
    roles: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    remote: bool | None = None
    internship: bool | None = None
    exclusions: tuple[str, ...] = ()


ActionKind = Literal[
    "submit_application",
    "send_reply",
    "send_outreach",
    "send_followup",
    "send_connection_invite",
    "accept_connection",
    "decline_connection",
]
ActionState = Literal[
    "proposed",
    "awaiting_approval",
    "blocked",
    "ready",
    "executing",
    "succeeded",
    "failed",
    "unknown",
    "cancelled",
]


class Policy(Contract):
    revision: int = Field(default=1, ge=1)
    mode: Literal["draft_only"] = "draft_only"
    application_cap: Literal[0] = 0
    communication_cap: Literal[0] = 0
    paused: bool = False
    restore_audit_required: bool = False


class ActionSnapshot(Contract):
    id: str = Field(default_factory=identifier)
    revision: int = Field(default=1, ge=1)
    kind: ActionKind
    target: str = Field(min_length=1)
    logical_key: str = Field(min_length=1)
    content: str
    fact_revisions: tuple[tuple[str, int], ...] = ()
    artifact_hashes: tuple[str, ...] = ()
    profile_revision: int = Field(ge=1)
    preferences_revision: int = Field(ge=1)
    policy_revision: int = Field(ge=1)

    @property
    def content_digest(self) -> str:
        return digest(self.model_dump_json().encode())


class ActionResult(Contract):
    outcome: Literal["succeeded", "failed", "unknown"]
    reason_code: str = Field(pattern=r"^[a-z_]+$")
    evidence_reference: str | None = None
    external_reference: str | None = None


class SourceObservation(Contract):
    source: Literal["greenhouse", "lever", "ashby"]
    external_id: str
    url: str
    title: str
    location: str | None = None
    description: str | None = None
    published_at: str | None = None
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: datetime = Field(default_factory=now)

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("observation time must be timezone aware")
        return value.astimezone(UTC)


class CollectionPage(Contract):
    items: tuple[SourceObservation, ...]
    coverage: Literal["complete", "partial", "unavailable"]
    next_cursor: str | None = None
    observed_at: datetime = Field(default_factory=now)


class Artifact(Contract):
    id: str = Field(default_factory=identifier)
    kind: Literal["baseline_cv", "tailored_cv", "cover_letter", "message_draft"]
    relative_path: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fact_revisions: tuple[tuple[str, int], ...] = ()
    profile_revision: int = Field(ge=1)
    opportunity_id: str | None = None
    generator_version: str


class WorkItem(Contract):
    id: str
    workflow: str
    due_at: datetime
    deadline: datetime
    attempts: int
    checkpoint: str | None
    payload: str

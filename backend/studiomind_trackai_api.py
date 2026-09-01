"""Strict StudioMind metadata intake for DrumTracKAI.

This boundary validates a provider-neutral generation request and returns a
deterministic metadata-validation receipt.  It deliberately does not enqueue
or generate a performance; a later task must bind the validated request to the
production generation engine and artifact lifecycle.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "1.1.0"
AUTH_ENVIRONMENT = "STUDIOMIND_TRACKAI_SANDBOX_AUTH"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,191}$")]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MusicalConstraints(ContractModel):
    abstract_style_tags: tuple[str, ...]
    named_artist_targets: tuple[str, ...] = ()
    prohibited_imitation: Literal[True]
    tempo_min_bpm: float = Field(gt=10.0, le=500.0)
    tempo_max_bpm: float = Field(gt=10.0, le=500.0)
    maximum_density: float = Field(ge=0.0, le=1.0)
    preserve_melody: bool
    preserve_lyrics: bool
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def safe_style_contract(self) -> "MusicalConstraints":
        if not self.abstract_style_tags:
            raise ValueError("at least one abstract style tag is required")
        if self.named_artist_targets:
            raise ValueError("named-artist generation targets are prohibited")
        if self.tempo_min_bpm > self.tempo_max_bpm:
            raise ValueError("tempo range is reversed")
        return self


class GenerationRequest(ContractModel):
    schema_version: Literal["1.0.0"]
    request_id: Identifier
    project_id: UUID
    production_brief_id: UUID
    production_brief_version: int = Field(ge=1)
    arrangement_plan_id: Identifier
    arrangement_revision: int = Field(ge=1)
    arrangement_fingerprint: Digest
    role_key: Identifier
    section_ids: tuple[Identifier, ...]
    capability_id: Identifier
    capability_fingerprint: Digest
    requested_artifact_kind: Literal["midi", "performance_description"]
    constraints: MusicalConstraints
    seed: int | None = Field(default=None, ge=0)
    parent_artifact_hashes: tuple[Digest, ...] = ()
    request_fingerprint: Digest
    automatic_submission_authorized: Literal[False]
    created_at: datetime

    @field_validator("section_ids")
    @classmethod
    def sections_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("at least one section is required")
        if len(set(value)) != len(value):
            raise ValueError("section IDs must be unique")
        return value

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


class StudioMindGenerationEnvelope(ContractModel):
    schema_version: Literal["1.1.0"]
    intent_id: Identifier
    request_id: Identifier
    request_fingerprint: Digest
    payload_digest: Digest
    production_role: Literal["drums", "percussion"]
    generation_request: GenerationRequest

    @model_validator(mode="after")
    def exact_binding(self) -> "StudioMindGenerationEnvelope":
        request = self.generation_request
        if (self.request_id, self.request_fingerprint) != (
            request.request_id,
            request.request_fingerprint,
        ):
            raise ValueError("envelope does not match its generation request")
        if self.payload_digest != stable_fingerprint(request.model_dump(mode="json")):
            raise ValueError("payload digest does not match the generation request")
        return self


class TrackAICapability(ContractModel):
    schema_version: Literal["1.1.0"] = SCHEMA_VERSION
    product: Literal["drumtrackai"] = "drumtrackai"
    provider_kind: Literal["track_ai"] = "track_ai"
    production_roles: tuple[Literal["drums", "percussion"], ...] = (
        "drums",
        "percussion",
    )
    artifact_kinds: tuple[Literal["midi", "performance_description"], ...] = (
        "midi",
        "performance_description",
    )
    supports_seed: Literal[True] = True
    prohibited_imitation_required: Literal[True] = True
    human_review_required: Literal[True] = True
    metadata_intake_available: Literal[True] = True
    generation_available_through_this_endpoint: Literal[False] = False
    artifact_access_available: Literal[False] = False
    automatic_dispatch_authorized: Literal[False] = False


class MetadataValidationReceipt(ContractModel):
    schema_version: Literal["1.1.0"] = SCHEMA_VERSION
    job_id: Identifier
    status: Literal["validated_metadata_only"] = "validated_metadata_only"
    intent_id: Identifier
    request_id: Identifier
    request_fingerprint: Digest
    payload_digest: Digest
    production_role: Literal["drums", "percussion"]
    requested_artifact_kind: Literal["midi", "performance_description"]
    generation_authorized: Literal[False] = False
    artifact_ready: Literal[False] = False
    candidate_commit_authorized: Literal[False] = False
    automatic_retry_authorized: Literal[False] = False


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_bearer(authorization: str | None) -> None:
    configured = os.getenv(AUTH_ENVIRONMENT, "")
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="StudioMind TrackAI intake is not configured",
        )
    scheme, separator, supplied = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not supplied
        or not secrets.compare_digest(supplied, configured)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid StudioMind TrackAI authorization",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(prefix="/v1/studiomind", tags=["studiomind-trackai"])


@router.get("/capabilities", response_model=TrackAICapability)
async def capabilities(
    authorization: Annotated[str | None, Header()] = None,
) -> TrackAICapability:
    _require_bearer(authorization)
    return TrackAICapability()


@router.post(
    "/generation-requests",
    response_model=MetadataValidationReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def validate_generation_request(
    payload: StudioMindGenerationEnvelope,
    authorization: Annotated[str | None, Header()] = None,
) -> MetadataValidationReceipt:
    _require_bearer(authorization)
    request = payload.generation_request
    job_digest = stable_fingerprint(
        {
            "contract": SCHEMA_VERSION,
            "intent_id": payload.intent_id,
            "request_id": request.request_id,
            "request_fingerprint": request.request_fingerprint,
            "payload_digest": payload.payload_digest,
            "production_role": payload.production_role,
        }
    )
    return MetadataValidationReceipt(
        job_id=f"drumtrackai-validation-{job_digest[:32]}",
        intent_id=payload.intent_id,
        request_id=request.request_id,
        request_fingerprint=request.request_fingerprint,
        payload_digest=payload.payload_digest,
        production_role=payload.production_role,
        requested_artifact_kind=request.requested_artifact_kind,
    )


__all__ = [
    "AUTH_ENVIRONMENT",
    "GenerationRequest",
    "MetadataValidationReceipt",
    "SCHEMA_VERSION",
    "StudioMindGenerationEnvelope",
    "TrackAICapability",
    "router",
    "stable_fingerprint",
]

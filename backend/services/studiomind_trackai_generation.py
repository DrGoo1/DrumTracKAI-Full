"""Compile validated StudioMind requests into reviewable DrumTracKAI plans.

This module deliberately stops before model invocation.  It closes the gap
between the provider-neutral StudioMind envelope and DrumTracKAI's production
engine by binding the exact arrangement, deterministic seed, and commercial
rights evidence into one immutable plan digest.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.studiomind_trackai_api import (
    Digest,
    Identifier,
    StudioMindGenerationEnvelope,
    expected_validation_job_id,
    stable_fingerprint,
)


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArrangementSection(PlanModel):
    section_id: Identifier
    section_type: Literal[
        "intro",
        "verse",
        "pre_chorus",
        "chorus",
        "bridge",
        "solo",
        "breakdown",
        "outro",
        "other",
    ]
    bars: int = Field(ge=1, le=512)
    energy: float = Field(ge=0.0, le=1.0)


class ArrangementContext(PlanModel):
    schema_version: Literal["1.0.0"]
    arrangement_fingerprint: Digest
    tempo_bpm: float = Field(gt=10.0, le=500.0)
    time_signature_numerator: int = Field(ge=1, le=32)
    time_signature_denominator: Literal[1, 2, 4, 8, 16, 32]
    sections: tuple[ArrangementSection, ...]

    @field_validator("sections")
    @classmethod
    def sections_are_nonempty_and_unique(
        cls, value: tuple[ArrangementSection, ...]
    ) -> tuple[ArrangementSection, ...]:
        if not value:
            raise ValueError("at least one arrangement section is required")
        identities = [section.section_id for section in value]
        if len(set(identities)) != len(identities):
            raise ValueError("arrangement section IDs must be unique")
        if sum(section.bars for section in value) > 4096:
            raise ValueError("arrangement exceeds the 4096-bar provider limit")
        return value


RightsBasis = Literal[
    "original_user_material",
    "licensed",
    "commissioned",
    "public_domain",
    "counsel_approved",
    "no_external_source",
]


class SourceRightsManifest(PlanModel):
    schema_version: Literal["1.0.0"]
    manifest_id: Identifier
    rights_basis: RightsBasis
    source_artifact_hashes: tuple[Digest, ...] = ()
    commercial_use_cleared: Literal[True]
    performer_identity_targeted: Literal[False]
    named_artist_generation_targeted: Literal[False]
    provenance_recorded: Literal[True]
    manifest_digest: Digest

    @model_validator(mode="after")
    def exact_digest_and_source_basis(self) -> "SourceRightsManifest":
        body = self.model_dump(mode="json", exclude={"manifest_digest"})
        if self.manifest_digest != stable_fingerprint(body):
            raise ValueError("rights manifest digest does not match its contents")
        if self.rights_basis == "no_external_source" and self.source_artifact_hashes:
            raise ValueError("no_external_source cannot declare source artifacts")
        if self.rights_basis != "no_external_source" and not self.source_artifact_hashes:
            raise ValueError("the selected rights basis requires a source artifact")
        return self


class GenerationPlanRequest(PlanModel):
    schema_version: Literal["1.0.0"]
    validation_job_id: Identifier
    envelope: StudioMindGenerationEnvelope
    arrangement: ArrangementContext
    rights_manifest: SourceRightsManifest
    human_review_required: Literal[True]
    automatic_execution_authorized: Literal[False]
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        return value


class DrumTracKAIProviderPayload(PlanModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    request_id: Identifier
    request_fingerprint: Digest
    production_role: Literal["drums", "percussion"]
    requested_artifact_kind: Literal["midi", "performance_description"]
    seed: int = Field(ge=0)
    arrangement: ArrangementContext
    abstract_style_tags: tuple[str, ...]
    maximum_density: float = Field(ge=0.0, le=1.0)
    parent_artifact_hashes: tuple[Digest, ...]
    rights_manifest_id: Identifier
    rights_manifest_digest: Digest


class GenerationPlanReceipt(PlanModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    status: Literal["prepared_for_human_review"] = "prepared_for_human_review"
    validation_job_id: Identifier
    generation_plan_id: Identifier
    generation_plan_digest: Digest
    intent_id: Identifier
    request_id: Identifier
    request_fingerprint: Digest
    payload_digest: Digest
    provider_payload: DrumTracKAIProviderPayload
    generation_authorized: Literal[False] = False
    artifact_ready: Literal[False] = False
    candidate_commit_authorized: Literal[False] = False
    automatic_retry_authorized: Literal[False] = False
    human_review_required: Literal[True] = True


def _validate_exact_bindings(request: GenerationPlanRequest) -> None:
    envelope = request.envelope
    generation = envelope.generation_request

    if request.validation_job_id != expected_validation_job_id(envelope):
        raise ValueError("validation job ID does not match the validated envelope")
    if request.arrangement.arrangement_fingerprint != generation.arrangement_fingerprint:
        raise ValueError("arrangement fingerprint does not match the generation request")

    supplied_sections = tuple(section.section_id for section in request.arrangement.sections)
    if supplied_sections != generation.section_ids:
        raise ValueError("arrangement sections do not exactly match the requested sections")

    constraints = generation.constraints
    if not constraints.tempo_min_bpm <= request.arrangement.tempo_bpm <= constraints.tempo_max_bpm:
        raise ValueError("arrangement tempo is outside the approved request range")
    if generation.seed is None:
        raise ValueError("a deterministic seed is required before provider preparation")

    source_hashes = request.rights_manifest.source_artifact_hashes
    if source_hashes != generation.parent_artifact_hashes:
        raise ValueError("rights manifest sources do not match parent artifact hashes")


def prepare_generation_plan(request: GenerationPlanRequest) -> GenerationPlanReceipt:
    """Return a deterministic, non-executing provider plan for human review."""

    _validate_exact_bindings(request)
    envelope = request.envelope
    generation = envelope.generation_request
    provider_payload = DrumTracKAIProviderPayload(
        request_id=generation.request_id,
        request_fingerprint=generation.request_fingerprint,
        production_role=envelope.production_role,
        requested_artifact_kind=generation.requested_artifact_kind,
        seed=generation.seed,
        arrangement=request.arrangement,
        abstract_style_tags=generation.constraints.abstract_style_tags,
        maximum_density=generation.constraints.maximum_density,
        parent_artifact_hashes=generation.parent_artifact_hashes,
        rights_manifest_id=request.rights_manifest.manifest_id,
        rights_manifest_digest=request.rights_manifest.manifest_digest,
    )
    plan_body: dict[str, Any] = {
        "contract": "drumtrackai-studiomind-generation-plan-v1",
        "validation_job_id": request.validation_job_id,
        "intent_id": envelope.intent_id,
        "request_id": generation.request_id,
        "request_fingerprint": generation.request_fingerprint,
        "payload_digest": envelope.payload_digest,
        "provider_payload": provider_payload.model_dump(mode="json"),
    }
    digest = stable_fingerprint(plan_body)
    return GenerationPlanReceipt(
        validation_job_id=request.validation_job_id,
        generation_plan_id=f"drumtrackai-plan-{digest[:32]}",
        generation_plan_digest=digest,
        intent_id=envelope.intent_id,
        request_id=generation.request_id,
        request_fingerprint=generation.request_fingerprint,
        payload_digest=envelope.payload_digest,
        provider_payload=provider_payload,
    )


__all__ = [
    "ArrangementContext",
    "ArrangementSection",
    "DrumTracKAIProviderPayload",
    "GenerationPlanReceipt",
    "GenerationPlanRequest",
    "SourceRightsManifest",
    "prepare_generation_plan",
]

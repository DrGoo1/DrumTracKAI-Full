"""One-time, human-approved execution of a reviewed DrumTracKAI plan."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.services.studiomind_trackai_generation import (
    DrumTracKAIProviderPayload,
    GenerationPlanReceipt,
)
from backend.studiomind_trackai_api import Digest, Identifier, stable_fingerprint


MAX_APPROVAL_LIFETIME = timedelta(minutes=15)
DEFAULT_MAX_MIDI_BYTES = 8 * 1024 * 1024


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GenerationApprovalReceipt(ExecutionModel):
    schema_version: Literal["1.0.0"]
    approval_id: Identifier
    reviewer_reference: Digest
    generation_plan_id: Identifier
    generation_plan_digest: Digest
    approved_action: Literal["generate_drum_candidate"]
    human_reviewed: Literal[True]
    single_use: Literal[True]
    automatic_execution_authorized: Literal[False]
    issued_at: datetime
    expires_at: datetime
    approval_digest: Digest

    @field_validator("issued_at", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def exact_digest_and_lifetime(self) -> "GenerationApprovalReceipt":
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiration must follow issuance")
        if self.expires_at - self.issued_at > MAX_APPROVAL_LIFETIME:
            raise ValueError("approval lifetime exceeds 15 minutes")
        body = self.model_dump(mode="json", exclude={"approval_digest"})
        if self.approval_digest != stable_fingerprint(body):
            raise ValueError("approval digest does not match its contents")
        return self


class GenerationExecutionRequest(ExecutionModel):
    schema_version: Literal["1.0.0"]
    plan: GenerationPlanReceipt
    approval: GenerationApprovalReceipt


class CandidateArtifact(ExecutionModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    artifact_kind: Literal["midi"] = "midi"
    content_base64: str
    sha256: Digest
    byte_length: int = Field(ge=14, le=DEFAULT_MAX_MIDI_BYTES)


class GenerationExecutionReceipt(ExecutionModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    status: Literal["candidate_ready_for_human_review"] = (
        "candidate_ready_for_human_review"
    )
    approval_id: Identifier
    generation_plan_id: Identifier
    generation_plan_digest: Digest
    request_id: Identifier
    request_fingerprint: Digest
    candidate_id: Identifier
    candidate_fingerprint: Digest
    artifact: CandidateArtifact
    provider_metadata: dict[str, Any]
    candidate_commit_authorized: Literal[False] = False
    automatic_retry_authorized: Literal[False] = False
    daw_execution_authorized: Literal[False] = False
    human_listening_review_required: Literal[True] = True


class ExecutionBindingError(ValueError):
    pass


class ApprovalExpiredError(RuntimeError):
    pass


class ApprovalReplayError(RuntimeError):
    pass


class GenerationBackendUnavailable(RuntimeError):
    pass


class ReplayGuard(Protocol):
    def consume(
        self, *, approval_id: str, approval_digest: str, generation_plan_digest: str
    ) -> bool: ...


class DrumGenerationClient(Protocol):
    def generate(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class SqliteReplayGuard:
    """Atomically consumes approvals before any provider side effect occurs."""

    def __init__(self, database_path: str | Path) -> None:
        raw = str(database_path or "").strip()
        if not raw:
            raise ValueError("replay database path is required")
        self._path = Path(raw)
        if not self._path.is_absolute():
            raise ValueError("replay database path must be absolute")
        if not self._path.parent.is_dir():
            raise ValueError("replay database parent directory does not exist")

    def consume(
        self, *, approval_id: str, approval_digest: str, generation_plan_digest: str
    ) -> bool:
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS studiomind_trackai_approval_uses (
                    approval_id TEXT PRIMARY KEY,
                    approval_digest TEXT NOT NULL UNIQUE,
                    generation_plan_digest TEXT NOT NULL UNIQUE,
                    consumed_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO studiomind_trackai_approval_uses (
                        approval_id, approval_digest, generation_plan_digest, consumed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        approval_digest,
                        generation_plan_digest,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                connection.commit()
                return True
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
        finally:
            connection.close()


class ProductionDrumGenerationClient:
    """HTTP-only adapter to DrumTracKAI's existing production route."""

    def __init__(self, *, base_url: str, timeout_seconds: float = 60.0) -> None:
        self._base_url = str(base_url or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("production generation base URL is required")
        if not self._base_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise ValueError("production generation URL must be loopback HTTP or HTTPS")
        self._timeout = max(1.0, min(float(timeout_seconds), 300.0))

    def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        import requests

        try:
            response = requests.post(
                f"{self._base_url}/v1/generate-drums",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except Exception as exc:
            raise GenerationBackendUnavailable(
                "DrumTracKAI production generation could not be reached"
            ) from exc
        if response.status_code >= 400:
            raise GenerationBackendUnavailable(
                f"DrumTracKAI production generation returned HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except Exception as exc:
            raise GenerationBackendUnavailable(
                "DrumTracKAI production generation returned invalid JSON"
            ) from exc
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise GenerationBackendUnavailable(
                "DrumTracKAI production generation did not return success"
            )
        return result


def expected_generation_plan_digest(plan: GenerationPlanReceipt) -> str:
    plan_body = {
        "contract": "drumtrackai-studiomind-generation-plan-v1",
        "validation_job_id": plan.validation_job_id,
        "intent_id": plan.intent_id,
        "request_id": plan.request_id,
        "request_fingerprint": plan.request_fingerprint,
        "payload_digest": plan.payload_digest,
        "provider_payload": plan.provider_payload.model_dump(mode="json"),
    }
    return stable_fingerprint(plan_body)


def compile_production_payload(
    provider: DrumTracKAIProviderPayload,
) -> dict[str, Any]:
    arrangement = provider.arrangement
    sections = [
        {
            "id": section.section_id,
            "name": section.section_type,
            "sectionType": section.section_type,
            "bars": section.bars,
            "energy": section.energy,
        }
        for section in arrangement.sections
    ]
    weighted_energy = sum(
        section.energy * section.bars for section in arrangement.sections
    ) / sum(section.bars for section in arrangement.sections)
    style = provider.abstract_style_tags[0]
    cfg = {
        "sectionId": provider.request_id,
        "songSections": sections,
        "tempo": arrangement.tempo_bpm,
        "tempos": [arrangement.tempo_bpm],
        "timeSignature": [
            arrangement.time_signature_numerator,
            arrangement.time_signature_denominator,
        ],
        "style": style,
        "styleTags": list(provider.abstract_style_tags),
        "intensity": round(weighted_energy, 6),
        "complexity": provider.maximum_density,
        "variation": 0.5,
        "generationMode": "full_ai",
        "humanize": True,
        "buildScope": "full_song",
        "seed": provider.seed,
    }
    return {
        "cfg": cfg,
        "songmap_summary": {
            "styleGroup": style,
            "sections": sections,
        },
        "drummer_profile": {},
    }


def _extract_midi(result: dict[str, Any], *, max_bytes: int) -> CandidateArtifact:
    encoded = result.get("midi_base64")
    if not encoded and isinstance(result.get("plugin_render"), dict):
        plugin_render = result["plugin_render"]
        encoded = (
            plugin_render.get("midi_base64")
            or plugin_render.get("midi_smf_base64")
            or plugin_render.get("midi_b64")
        )
    if not isinstance(encoded, str) or not encoded:
        raise GenerationBackendUnavailable("production generation returned no MIDI artifact")
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 4:
        raise GenerationBackendUnavailable("production MIDI artifact exceeds the size limit")
    try:
        midi = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GenerationBackendUnavailable("production MIDI artifact is not valid base64") from exc
    if len(midi) < 14 or len(midi) > max_bytes or not midi.startswith(b"MThd"):
        raise GenerationBackendUnavailable("production artifact is not a bounded MIDI file")
    canonical = base64.b64encode(midi).decode("ascii")
    return CandidateArtifact(
        content_base64=canonical,
        sha256=hashlib.sha256(midi).hexdigest(),
        byte_length=len(midi),
    )


def _safe_provider_metadata(result: dict[str, Any]) -> dict[str, Any]:
    raw = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    allowed = (
        "backend",
        "model_version",
        "version",
        "render_source",
        "sentient_routed",
    )
    return {key: raw[key] for key in allowed if key in raw}


@dataclass
class StudioMindGenerationExecutor:
    client: DrumGenerationClient
    replay_guard: ReplayGuard
    max_midi_bytes: int = DEFAULT_MAX_MIDI_BYTES

    def execute(
        self,
        request: GenerationExecutionRequest,
        *,
        now: datetime | None = None,
    ) -> GenerationExecutionReceipt:
        plan = request.plan
        approval = request.approval
        expected_digest = expected_generation_plan_digest(plan)
        if plan.generation_plan_digest != expected_digest:
            raise ExecutionBindingError("generation plan digest does not match its contents")
        if plan.generation_plan_id != f"drumtrackai-plan-{expected_digest[:32]}":
            raise ExecutionBindingError("generation plan ID does not match its contents")
        if (
            approval.generation_plan_id != plan.generation_plan_id
            or approval.generation_plan_digest != plan.generation_plan_digest
        ):
            raise ExecutionBindingError("approval is not bound to the generation plan")

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("execution time must be timezone-aware")
        if current < approval.issued_at or current > approval.expires_at:
            raise ApprovalExpiredError("generation approval is not currently valid")

        consumed = self.replay_guard.consume(
            approval_id=approval.approval_id,
            approval_digest=approval.approval_digest,
            generation_plan_digest=plan.generation_plan_digest,
        )
        if not consumed:
            raise ApprovalReplayError("generation approval or plan was already consumed")

        production_payload = compile_production_payload(plan.provider_payload)
        result = self.client.generate(production_payload)
        max_bytes = max(14, min(int(self.max_midi_bytes), DEFAULT_MAX_MIDI_BYTES))
        artifact = _extract_midi(result, max_bytes=max_bytes)
        candidate_body = {
            "contract": "drumtrackai-studiomind-candidate-v1",
            "generation_plan_digest": plan.generation_plan_digest,
            "request_fingerprint": plan.request_fingerprint,
            "artifact_sha256": artifact.sha256,
            "artifact_bytes": artifact.byte_length,
            "provider_metadata": _safe_provider_metadata(result),
        }
        fingerprint = stable_fingerprint(candidate_body)
        return GenerationExecutionReceipt(
            approval_id=approval.approval_id,
            generation_plan_id=plan.generation_plan_id,
            generation_plan_digest=plan.generation_plan_digest,
            request_id=plan.request_id,
            request_fingerprint=plan.request_fingerprint,
            candidate_id=f"drumtrackai-candidate-{fingerprint[:32]}",
            candidate_fingerprint=fingerprint,
            artifact=artifact,
            provider_metadata=candidate_body["provider_metadata"],
        )


def build_configured_executor() -> StudioMindGenerationExecutor:
    if os.getenv("STUDIOMIND_TRACKAI_GENERATION_ENABLED", "").strip().lower() != "true":
        raise GenerationBackendUnavailable("StudioMind generation is disabled")
    base_url = os.getenv("DRUMTRACKAI_GENERATION_API_BASE", "")
    replay_path = os.getenv("STUDIOMIND_TRACKAI_REPLAY_DB_PATH", "")
    timeout = float(os.getenv("STUDIOMIND_TRACKAI_GENERATION_TIMEOUT_SECONDS", "60"))
    max_bytes = int(
        os.getenv("STUDIOMIND_TRACKAI_MAX_MIDI_BYTES", str(DEFAULT_MAX_MIDI_BYTES))
    )
    return StudioMindGenerationExecutor(
        client=ProductionDrumGenerationClient(
            base_url=base_url,
            timeout_seconds=timeout,
        ),
        replay_guard=SqliteReplayGuard(replay_path),
        max_midi_bytes=max_bytes,
    )


__all__ = [
    "ApprovalExpiredError",
    "ApprovalReplayError",
    "CandidateArtifact",
    "ExecutionBindingError",
    "GenerationApprovalReceipt",
    "GenerationBackendUnavailable",
    "GenerationExecutionReceipt",
    "GenerationExecutionRequest",
    "ProductionDrumGenerationClient",
    "SqliteReplayGuard",
    "StudioMindGenerationExecutor",
    "build_configured_executor",
    "compile_production_payload",
    "expected_generation_plan_digest",
]

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from backend.services.studiomind_trackai_execution import (
    ApprovalExpiredError,
    ApprovalReplayError,
    ExecutionBindingError,
    GenerationApprovalReceipt,
    GenerationBackendUnavailable,
    GenerationExecutionRequest,
    SqliteReplayGuard,
    StudioMindGenerationExecutor,
    build_configured_executor,
    compile_production_payload,
)
from backend.services.studiomind_trackai_generation import (
    GenerationPlanRequest,
    prepare_generation_plan,
)
from backend.studiomind_trackai_api import stable_fingerprint


HEX_A = "a" * 64
HEX_B = "b" * 64
MIDI = b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0"


class MemoryReplayGuard:
    def __init__(self) -> None:
        self.values: set[str] = set()

    def consume(
        self, *, approval_id: str, approval_digest: str, generation_plan_digest: str
    ) -> bool:
        keys = {approval_id, approval_digest, generation_plan_digest}
        if self.values.intersection(keys):
            return False
        self.values.update(keys)
        return True


class FakeClient:
    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or {
            "ok": True,
            "midi_base64": base64.b64encode(MIDI).decode("ascii"),
            "metadata": {
                "backend": "onnx",
                "model_version": "test-model-1",
                "sentient_routed": False,
                "private_path": "/must/not/escape",
            },
        }

    def generate(self, payload: dict) -> dict:
        self.calls.append(payload)
        return self.result


def _plan():
    now = datetime.now(timezone.utc)
    generation = {
        "schema_version": "1.0.0",
        "request_id": "request-1",
        "project_id": str(uuid4()),
        "production_brief_id": str(uuid4()),
        "production_brief_version": 1,
        "arrangement_plan_id": "arrangement-1",
        "arrangement_revision": 1,
        "arrangement_fingerprint": HEX_A,
        "role_key": "rhythm.drums.main",
        "section_ids": ["verse-1", "chorus-1"],
        "capability_id": "drumtrackai-midi-v1",
        "capability_fingerprint": HEX_A,
        "requested_artifact_kind": "midi",
        "constraints": {
            "abstract_style_tags": ["driving", "dynamic"],
            "named_artist_targets": [],
            "prohibited_imitation": True,
            "tempo_min_bpm": 118.0,
            "tempo_max_bpm": 122.0,
            "maximum_density": 0.7,
            "preserve_melody": True,
            "preserve_lyrics": True,
            "notes": [],
        },
        "seed": 42,
        "parent_artifact_hashes": [HEX_B],
        "request_fingerprint": HEX_B,
        "automatic_submission_authorized": False,
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }
    envelope = {
        "schema_version": "1.1.0",
        "intent_id": "intent-1",
        "request_id": generation["request_id"],
        "request_fingerprint": generation["request_fingerprint"],
        "payload_digest": stable_fingerprint(generation),
        "production_role": "drums",
        "generation_request": generation,
    }
    validation_body = {
        "contract": "1.1.0",
        "intent_id": envelope["intent_id"],
        "request_id": generation["request_id"],
        "request_fingerprint": generation["request_fingerprint"],
        "payload_digest": envelope["payload_digest"],
        "production_role": envelope["production_role"],
    }
    validation_job_id = (
        f"drumtrackai-validation-{stable_fingerprint(validation_body)[:32]}"
    )
    rights = {
        "schema_version": "1.0.0",
        "manifest_id": "rights-1",
        "rights_basis": "original_user_material",
        "source_artifact_hashes": [HEX_B],
        "commercial_use_cleared": True,
        "performer_identity_targeted": False,
        "named_artist_generation_targeted": False,
        "provenance_recorded": True,
    }
    rights["manifest_digest"] = stable_fingerprint(rights)
    request = GenerationPlanRequest.model_validate(
        {
            "schema_version": "1.0.0",
            "validation_job_id": validation_job_id,
            "envelope": envelope,
            "arrangement": {
                "schema_version": "1.0.0",
                "arrangement_fingerprint": HEX_A,
                "tempo_bpm": 120.0,
                "time_signature_numerator": 4,
                "time_signature_denominator": 4,
                "sections": [
                    {
                        "section_id": "verse-1",
                        "section_type": "verse",
                        "bars": 8,
                        "energy": 0.5,
                    },
                    {
                        "section_id": "chorus-1",
                        "section_type": "chorus",
                        "bars": 8,
                        "energy": 0.8,
                    },
                ],
            },
            "rights_manifest": rights,
            "human_review_required": True,
            "automatic_execution_authorized": False,
            "requested_at": now.isoformat(),
        }
    )
    return prepare_generation_plan(request)


def _approval(plan, *, issued_at: datetime | None = None, lifetime_minutes: int = 5):
    issued = issued_at or datetime.now(timezone.utc)
    body = {
        "schema_version": "1.0.0",
        "approval_id": "approval-1",
        "reviewer_reference": "c" * 64,
        "generation_plan_id": plan.generation_plan_id,
        "generation_plan_digest": plan.generation_plan_digest,
        "approved_action": "generate_drum_candidate",
        "human_reviewed": True,
        "single_use": True,
        "automatic_execution_authorized": False,
        "issued_at": issued.isoformat().replace("+00:00", "Z"),
        "expires_at": (issued + timedelta(minutes=lifetime_minutes))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    body["approval_digest"] = stable_fingerprint(body)
    return GenerationApprovalReceipt.model_validate(body)


def test_execution_invokes_existing_drum_generation_once_and_returns_review_candidate() -> None:
    plan = _plan()
    approval = _approval(plan)
    client = FakeClient()
    executor = StudioMindGenerationExecutor(client=client, replay_guard=MemoryReplayGuard())

    receipt = executor.execute(
        GenerationExecutionRequest(
            schema_version="1.0.0", plan=plan, approval=approval
        )
    )

    assert len(client.calls) == 1
    compiled = client.calls[0]
    assert compiled["cfg"]["songSections"][1]["name"] == "chorus"
    assert compiled["cfg"]["seed"] == 42
    assert receipt.status == "candidate_ready_for_human_review"
    assert receipt.artifact.content_base64 == base64.b64encode(MIDI).decode("ascii")
    assert receipt.artifact.byte_length == len(MIDI)
    assert receipt.provider_metadata == {
        "backend": "onnx",
        "model_version": "test-model-1",
        "sentient_routed": False,
    }
    assert receipt.candidate_commit_authorized is False
    assert receipt.automatic_retry_authorized is False
    assert receipt.daw_execution_authorized is False
    assert receipt.human_listening_review_required is True


def test_approval_and_plan_are_single_use() -> None:
    plan = _plan()
    approval = _approval(plan)
    executor = StudioMindGenerationExecutor(
        client=FakeClient(), replay_guard=MemoryReplayGuard()
    )
    request = GenerationExecutionRequest(
        schema_version="1.0.0", plan=plan, approval=approval
    )
    executor.execute(request)
    with pytest.raises(ApprovalReplayError):
        executor.execute(request)


def test_expired_approval_is_rejected_before_replay_consumption() -> None:
    plan = _plan()
    issued = datetime.now(timezone.utc) - timedelta(minutes=10)
    approval = _approval(plan, issued_at=issued, lifetime_minutes=5)
    client = FakeClient()
    guard = MemoryReplayGuard()
    executor = StudioMindGenerationExecutor(client=client, replay_guard=guard)
    with pytest.raises(ApprovalExpiredError):
        executor.execute(
            GenerationExecutionRequest(
                schema_version="1.0.0", plan=plan, approval=approval
            )
        )
    assert client.calls == []
    assert guard.values == set()


def test_tampered_plan_is_rejected_before_provider_call() -> None:
    plan = _plan()
    approval = _approval(plan)
    raw = plan.model_dump(mode="json")
    raw["provider_payload"]["maximum_density"] = 0.1
    client = FakeClient()
    executor = StudioMindGenerationExecutor(client=client, replay_guard=MemoryReplayGuard())
    with pytest.raises(ExecutionBindingError):
        executor.execute(
            GenerationExecutionRequest.model_validate(
                {"schema_version": "1.0.0", "plan": raw, "approval": approval}
            )
        )
    assert client.calls == []


def test_invalid_or_missing_midi_fails_closed_after_consuming_approval() -> None:
    plan = _plan()
    approval = _approval(plan)
    guard = MemoryReplayGuard()
    executor = StudioMindGenerationExecutor(
        client=FakeClient({"ok": True, "midi_base64": "bm90LW1pZGk="}),
        replay_guard=guard,
    )
    request = GenerationExecutionRequest(
        schema_version="1.0.0", plan=plan, approval=approval
    )
    with pytest.raises(GenerationBackendUnavailable):
        executor.execute(request)
    with pytest.raises(ApprovalReplayError):
        executor.execute(request)


def test_sqlite_replay_guard_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "approvals.sqlite3"
    first = SqliteReplayGuard(path)
    second = SqliteReplayGuard(path)
    values = {
        "approval_id": "approval-1",
        "approval_digest": "d" * 64,
        "generation_plan_digest": "e" * 64,
    }
    assert first.consume(**values) is True
    assert second.consume(**values) is False


def test_production_payload_is_provider_neutral_and_deterministic() -> None:
    provider = _plan().provider_payload
    first = compile_production_payload(provider)
    second = compile_production_payload(provider)
    assert first == second
    assert first["cfg"]["intensity"] == 0.65
    assert first["cfg"]["style"] == "driving"
    assert "named_artist" not in str(first).lower()


def test_default_execution_is_disabled(monkeypatch) -> None:
    monkeypatch.delenv("STUDIOMIND_TRACKAI_GENERATION_ENABLED", raising=False)
    monkeypatch.delenv("DRUMTRACKAI_GENERATION_API_BASE", raising=False)
    monkeypatch.delenv("STUDIOMIND_TRACKAI_REPLAY_DB_PATH", raising=False)
    with pytest.raises(GenerationBackendUnavailable):
        build_configured_executor()

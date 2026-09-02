from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studiomind_trackai_api import router as intake_router, stable_fingerprint
from backend.studiomind_trackai_generation_api import router as plan_router


AUTH_VALUE = "non-production-test-value"
HEX_A = "a" * 64
HEX_B = "b" * 64


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(intake_router)
    app.include_router(plan_router)
    return TestClient(app)


def _envelope(*, seed: int | None = 42) -> dict:
    request = {
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
            "abstract_style_tags": ["driving", "dynamic", "syncopated"],
            "named_artist_targets": [],
            "prohibited_imitation": True,
            "tempo_min_bpm": 118.0,
            "tempo_max_bpm": 122.0,
            "maximum_density": 0.7,
            "preserve_melody": True,
            "preserve_lyrics": True,
            "notes": [],
        },
        "seed": seed,
        "parent_artifact_hashes": [HEX_B],
        "request_fingerprint": HEX_B,
        "automatic_submission_authorized": False,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return {
        "schema_version": "1.1.0",
        "intent_id": "intent-1",
        "request_id": request["request_id"],
        "request_fingerprint": request["request_fingerprint"],
        "payload_digest": stable_fingerprint(request),
        "production_role": "drums",
        "generation_request": request,
    }


def _rights_manifest() -> dict:
    manifest = {
        "schema_version": "1.0.0",
        "manifest_id": "rights-1",
        "rights_basis": "original_user_material",
        "source_artifact_hashes": [HEX_B],
        "commercial_use_cleared": True,
        "performer_identity_targeted": False,
        "named_artist_generation_targeted": False,
        "provenance_recorded": True,
    }
    manifest["manifest_digest"] = stable_fingerprint(manifest)
    return manifest


def _plan_request(*, seed: int | None = 42) -> dict:
    envelope = _envelope(seed=seed)
    return {
        "schema_version": "1.0.0",
        "validation_job_id": "placeholder",
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
                    "energy": 0.55,
                },
                {
                    "section_id": "chorus-1",
                    "section_type": "chorus",
                    "bars": 8,
                    "energy": 0.82,
                },
            ],
        },
        "rights_manifest": _rights_manifest(),
        "human_review_required": True,
        "automatic_execution_authorized": False,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


def _validated_plan_request(client: TestClient, monkeypatch, *, seed: int | None = 42) -> dict:
    monkeypatch.setenv("STUDIOMIND_TRACKAI_SANDBOX_AUTH", AUTH_VALUE)
    payload = _plan_request(seed=seed)
    response = client.post(
        "/v1/studiomind/generation-requests",
        json=payload["envelope"],
        headers={"Authorization": f"Bearer {AUTH_VALUE}"},
    )
    assert response.status_code == 202
    payload["validation_job_id"] = response.json()["job_id"]
    return payload


def test_generation_plan_is_deterministic_and_remains_non_executing(monkeypatch) -> None:
    client = _client()
    payload = _validated_plan_request(client, monkeypatch)
    headers = {"Authorization": f"Bearer {AUTH_VALUE}"}

    first = client.post("/v1/studiomind/generation-plans", json=payload, headers=headers)
    second_payload = deepcopy(payload)
    second_payload["requested_at"] = datetime.now(timezone.utc).isoformat()
    second = client.post(
        "/v1/studiomind/generation-plans", json=second_payload, headers=headers
    )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    receipt = first.json()
    assert receipt["status"] == "prepared_for_human_review"
    assert receipt["generation_plan_id"].startswith("drumtrackai-plan-")
    assert receipt["generation_authorized"] is False
    assert receipt["artifact_ready"] is False
    assert receipt["candidate_commit_authorized"] is False
    assert receipt["automatic_retry_authorized"] is False
    assert receipt["human_review_required"] is True
    assert receipt["provider_payload"]["seed"] == 42
    assert receipt["provider_payload"]["arrangement"]["sections"][1]["energy"] == 0.82
    assert "source_artifact_hashes" not in receipt["provider_payload"]


def test_generation_plan_requires_authentication(monkeypatch) -> None:
    client = _client()
    payload = _validated_plan_request(client, monkeypatch)
    response = client.post("/v1/studiomind/generation-plans", json=payload)
    assert response.status_code == 401
    assert AUTH_VALUE not in response.text


def test_exact_validation_arrangement_tempo_and_sources_are_required(monkeypatch) -> None:
    client = _client()
    headers = {"Authorization": f"Bearer {AUTH_VALUE}"}
    original = _validated_plan_request(client, monkeypatch)

    mutations = []
    wrong_job = deepcopy(original)
    wrong_job["validation_job_id"] = "drumtrackai-validation-wrong"
    mutations.append(wrong_job)

    wrong_fingerprint = deepcopy(original)
    wrong_fingerprint["arrangement"]["arrangement_fingerprint"] = "c" * 64
    mutations.append(wrong_fingerprint)

    reordered_sections = deepcopy(original)
    reordered_sections["arrangement"]["sections"].reverse()
    mutations.append(reordered_sections)

    out_of_range_tempo = deepcopy(original)
    out_of_range_tempo["arrangement"]["tempo_bpm"] = 130.0
    mutations.append(out_of_range_tempo)

    wrong_sources = deepcopy(original)
    wrong_sources["rights_manifest"] = {
        **wrong_sources["rights_manifest"],
        "source_artifact_hashes": ["c" * 64],
    }
    manifest_body = {
        key: value
        for key, value in wrong_sources["rights_manifest"].items()
        if key != "manifest_digest"
    }
    wrong_sources["rights_manifest"]["manifest_digest"] = stable_fingerprint(manifest_body)
    mutations.append(wrong_sources)

    for payload in mutations:
        response = client.post(
            "/v1/studiomind/generation-plans", json=payload, headers=headers
        )
        assert response.status_code == 422


def test_generation_plan_rejects_missing_seed_or_tampered_rights(monkeypatch) -> None:
    client = _client()
    headers = {"Authorization": f"Bearer {AUTH_VALUE}"}

    no_seed = _validated_plan_request(client, monkeypatch, seed=None)
    response = client.post(
        "/v1/studiomind/generation-plans", json=no_seed, headers=headers
    )
    assert response.status_code == 422
    assert "deterministic seed" in response.text

    tampered = _validated_plan_request(client, monkeypatch)
    tampered["rights_manifest"]["commercial_use_cleared"] = False
    response = client.post(
        "/v1/studiomind/generation-plans", json=tampered, headers=headers
    )
    assert response.status_code == 422


def test_no_external_source_manifest_cannot_hide_parent_artifacts(monkeypatch) -> None:
    client = _client()
    payload = _validated_plan_request(client, monkeypatch)
    manifest = {
        **payload["rights_manifest"],
        "rights_basis": "no_external_source",
    }
    manifest["manifest_digest"] = stable_fingerprint(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    )
    payload["rights_manifest"] = manifest
    response = client.post(
        "/v1/studiomind/generation-plans",
        json=payload,
        headers={"Authorization": f"Bearer {AUTH_VALUE}"},
    )
    assert response.status_code == 422

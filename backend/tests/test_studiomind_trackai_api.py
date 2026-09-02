from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.studiomind_trackai_api import (
    AUTH_ENVIRONMENT,
    router,
    stable_fingerprint,
)
from backend.studiomind_trackai_generation_api import router as generation_plan_router


AUTH_VALUE = "non-production-test-value"
HEX = "a" * 64


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.include_router(generation_plan_router)
    return TestClient(app)


def _payload() -> dict:
    request = {
        "schema_version": "1.0.0",
        "request_id": "request-1",
        "project_id": str(uuid4()),
        "production_brief_id": str(uuid4()),
        "production_brief_version": 1,
        "arrangement_plan_id": "arrangement-1",
        "arrangement_revision": 1,
        "arrangement_fingerprint": HEX,
        "role_key": "rhythm.drums.main",
        "section_ids": ["verse-1", "chorus-1"],
        "capability_id": "drumtrackai-midi-v1",
        "capability_fingerprint": HEX,
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
        "seed": 42,
        "parent_artifact_hashes": [],
        "request_fingerprint": "b" * 64,
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


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AUTH_VALUE}"}


def test_intake_fails_closed_when_auth_is_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv(AUTH_ENVIRONMENT, raising=False)
    response = _client().post(
        "/v1/studiomind/generation-requests", json=_payload(), headers=_headers()
    )
    assert response.status_code == 503
    assert AUTH_VALUE not in response.text


def test_intake_rejects_incorrect_auth_without_disclosure(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENVIRONMENT, AUTH_VALUE)
    response = _client().post(
        "/v1/studiomind/generation-requests",
        json=_payload(),
        headers={"Authorization": "Bearer wrong-value"},
    )
    assert response.status_code == 401
    assert AUTH_VALUE not in response.text


def test_valid_drum_request_returns_deterministic_metadata_receipt(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENVIRONMENT, AUTH_VALUE)
    payload = _payload()
    first = _client().post(
        "/v1/studiomind/generation-requests", json=payload, headers=_headers()
    )
    second = _client().post(
        "/v1/studiomind/generation-requests", json=payload, headers=_headers()
    )
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    receipt = first.json()
    assert receipt["status"] == "validated_metadata_only"
    assert receipt["job_id"].startswith("drumtrackai-validation-")
    assert receipt["generation_authorized"] is False
    assert receipt["artifact_ready"] is False
    assert receipt["candidate_commit_authorized"] is False
    assert receipt["automatic_retry_authorized"] is False


def test_exact_digest_and_request_binding_are_required(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENVIRONMENT, AUTH_VALUE)
    for mutation in ("digest", "request", "extra"):
        payload = deepcopy(_payload())
        if mutation == "digest":
            payload["payload_digest"] = "c" * 64
        elif mutation == "request":
            payload["request_id"] = "request-other"
        else:
            payload["unexpected"] = True
        response = _client().post(
            "/v1/studiomind/generation-requests", json=payload, headers=_headers()
        )
        assert response.status_code == 422


def test_named_artist_automatic_submission_and_non_drum_role_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENVIRONMENT, AUTH_VALUE)
    payloads = []

    artist = deepcopy(_payload())
    artist["generation_request"]["constraints"]["named_artist_targets"] = ["Named Artist"]
    artist["payload_digest"] = stable_fingerprint(artist["generation_request"])
    payloads.append(artist)

    automatic = deepcopy(_payload())
    automatic["generation_request"]["automatic_submission_authorized"] = True
    automatic["payload_digest"] = stable_fingerprint(automatic["generation_request"])
    payloads.append(automatic)

    role = deepcopy(_payload())
    role["production_role"] = "bass"
    payloads.append(role)

    for payload in payloads:
        response = _client().post(
            "/v1/studiomind/generation-requests", json=payload, headers=_headers()
        )
        assert response.status_code == 422


def test_capability_is_drum_scoped_and_non_executing(monkeypatch) -> None:
    monkeypatch.setenv(AUTH_ENVIRONMENT, AUTH_VALUE)
    response = _client().get("/v1/studiomind/capabilities", headers=_headers())
    assert response.status_code == 200
    capability = response.json()
    assert capability["production_roles"] == ["drums", "percussion"]
    assert capability["generation_plan_preparation_available"] is True
    assert capability["generation_available_through_this_endpoint"] is False
    assert capability["artifact_access_available"] is False
    assert capability["human_review_required"] is True
    assert capability["prohibited_imitation_required"] is True

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from jose import jwt
from fastapi import HTTPException

from backend import calibration_v2_api
from backend.security.supabase_auth import verify_token
from backend.services.calibration_profile_resolver import validate_profile_overrides
from backend.services.calibration_trial_service import assign_visible_roles
from backend.workers.calibration_render_worker import CalibrationRenderWorker


def _token(secret: str, *, subject: str = "user-1") -> str:
    return jwt.encode(
        {
            "sub": subject,
            "aud": "authenticated",
            "iss": "https://example.supabase.co/auth/v1",
            "exp": int(time.time()) + 300,
        },
        secret,
        algorithm="HS256",
    )


def test_valid_supabase_hmac_token_is_verified(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_UNVERIFIED_JWT", "false")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://example.supabase.co/auth/v1")
    claims = verify_token(_token("test-secret"))
    assert claims["sub"] == "user-1"


def test_invalid_supabase_token_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("ALLOW_UNVERIFIED_JWT", "false")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SUPABASE_JWT_ISSUER", "https://example.supabase.co/auth/v1")
    with pytest.raises(HTTPException) as error:
        verify_token(_token("wrong-secret"))
    assert error.value.status_code == 401


def test_unverified_tokens_are_local_only(monkeypatch):
    token = _token("different-secret")
    monkeypatch.setenv("ALLOW_UNVERIFIED_JWT", "true")
    monkeypatch.setenv("APP_ENV", "development")
    assert verify_token(token)["sub"] == "user-1"
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError):
        verify_token(token)


def test_assignment_seed_is_deterministic_and_counterbalanced():
    assignments = {assign_visible_roles(seed) for seed in range(64)}
    assert assignments == {("control", "challenger"), ("challenger", "control")}
    assert assign_visible_roles(17) == assign_visible_roles(17)


def test_profile_treatment_overrides_are_bounded():
    assert validate_profile_overrides(
        {"phase32_42_features": {"phase37_42": {"drummer_personality_profile": {"chaos": 0.5}}}}
    )
    with pytest.raises(ValueError):
        validate_profile_overrides({"profile_version": "attacker-controlled"})
    with pytest.raises(ValueError):
        validate_profile_overrides(
            {"phase32_42_features": {"phase37_42": {"drummer_personality_profile": {"chaos": 2}}}}
        )


class _ReviewerRepo:
    def __init__(self, roles):
        self._roles = set(roles)
        self.context = SimpleNamespace(
            reviewer_id="reviewer-1",
            auth_user_id="user-1",
            display_name="Internal Reviewer",
            expertise_level="professional",
            consent_version="v1",
            consented_at="2026-01-01T00:00:00Z",
            is_active=True,
        )

    def require_reviewer(self, user_id):
        assert user_id == "user-1"
        return self.context

    def roles_for_user(self, user_id):
        assert user_id == "user-1"
        return set(self._roles)


def _authenticated_user():
    return SimpleNamespace(user_id="user-1")


def test_verified_internal_reviewer_can_access_while_external_gate_is_closed(monkeypatch):
    monkeypatch.setenv("CALIBRATION_V2_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_INTERNAL_REVIEWERS_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_EXTERNAL_REVIEWERS_ENABLED", "false")

    context = calibration_v2_api._require_reviewer_context(
        user=_authenticated_user(),
        repo=_ReviewerRepo({"internal_reviewer"}),
    )

    assert context.reviewer_id == "reviewer-1"


def test_internal_reviewer_gate_requires_an_allowed_role(monkeypatch):
    monkeypatch.setenv("CALIBRATION_V2_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_INTERNAL_REVIEWERS_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_EXTERNAL_REVIEWERS_ENABLED", "false")

    with pytest.raises(HTTPException) as error:
        calibration_v2_api._require_reviewer_context(
            user=_authenticated_user(),
            repo=_ReviewerRepo({"reviewer"}),
        )

    assert error.value.status_code == 503


def test_external_gate_allows_active_reviewer_without_internal_role(monkeypatch):
    monkeypatch.setenv("CALIBRATION_V2_ENABLED", "true")
    monkeypatch.setenv("CALIBRATION_INTERNAL_REVIEWERS_ENABLED", "false")
    monkeypatch.setenv("CALIBRATION_EXTERNAL_REVIEWERS_ENABLED", "true")

    context = calibration_v2_api._require_reviewer_context(
        user=_authenticated_user(),
        repo=_ReviewerRepo(set()),
    )

    assert context.reviewer_id == "reviewer-1"


class _WorkerDb:
    def __init__(self):
        self.status_updates = []
        self.run_updates = []

    def get_calibration_run(self, *, run_id):
        return SimpleNamespace(run_id=run_id, drummer_slug="test", metadata={}, started_at=None)

    def update_calibration_render_job_status(self, **kwargs):
        self.status_updates.append(kwargs)
        return True

    def log_calibration_run(self, **kwargs):
        self.run_updates.append(kwargs)
        return kwargs.get("run_id")


def test_renderer_failure_does_not_complete_job(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    db = _WorkerDb()
    worker = CalibrationRenderWorker(db, command_template="", max_retries=1)
    result = worker._process_claimed_job(
        SimpleNamespace(job_id="job-1", run_id="run-1", render_profile_id="profile", sample_pack_version="pack")
    )
    assert result == "failed"
    assert db.status_updates[-1]["status"] == "failed"
    assert db.run_updates[-1]["outcome"] == "failure"
    assert not worker._storage_uri_allowed("C:/local/preview.wav")
    assert worker._storage_uri_allowed("s3://bucket/object.wav")


def test_staging_also_rejects_worker_local_artifacts(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    worker = CalibrationRenderWorker(_WorkerDb(), command_template="unused")
    assert not worker._storage_uri_allowed("/opt/render/project/src/artifacts/preview.wav")
    assert not worker._storage_uri_allowed("file:///tmp/preview.wav")
    assert worker._storage_uri_allowed("s3://bucket/object.wav")
    assert worker._storage_uri_allowed("https://example.test/object.wav")

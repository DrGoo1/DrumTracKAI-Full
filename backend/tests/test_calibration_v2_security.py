from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from jose import jwt
from fastapi import HTTPException

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

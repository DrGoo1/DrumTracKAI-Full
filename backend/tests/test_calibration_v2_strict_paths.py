from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from admin.services.central_database_service import CalibrationRenderJob
from backend.calibration_v2_api import _http_error
from backend.services.calibration_production_engine import ProductionCandidate
from backend.services.calibration_render_service import CalibrationRenderService, RenderRequest
from backend.services.calibration_trial_service import (
    CalibrationDependencyError,
    CalibrationTrialService,
    TrialCreateInput,
)
from backend.services.calibration_v2_repository import CalibrationV2Repository
from backend.services.production_performance_client import ProductionGenerationUnavailable
from backend.workers.calibration_render_worker import CalibrationRenderWorker


@dataclass
class _FakeRun:
    run_id: str
    drummer_slug: str
    metadata: Dict[str, Any]
    started_at: Optional[datetime] = None


class _TrialDbDouble:
    def __init__(self) -> None:
        self.runs: Dict[str, _FakeRun] = {}
        self.render_jobs: List[Dict[str, Any]] = []
        self.run_log_calls = 0
        self.session_calls = 0
        self.item_calls = 0

    def log_calibration_run(self, *, run_id: str, drummer_slug: str, metadata: Dict[str, Any], **_: Any) -> str:
        self.run_log_calls += 1
        self.runs[run_id] = _FakeRun(run_id=run_id, drummer_slug=drummer_slug, metadata=dict(metadata or {}))
        return run_id

    def get_calibration_run(self, *, run_id: str) -> Optional[_FakeRun]:
        return self.runs.get(run_id)

    def upsert_run_version(self, **_: Any) -> bool:
        return True

    def upsert_calibration_run_events(self, **_: Any) -> bool:
        return True

    def log_calibration_render_job(self, *, run_id: str, render_profile_id: str, sample_pack_version: str, **_: Any) -> str:
        job_id = f"rjob_{len(self.render_jobs) + 1}"
        self.render_jobs.append(
            {
                "job_id": job_id,
                "run_id": run_id,
                "render_profile_id": render_profile_id,
                "sample_pack_version": sample_pack_version,
            }
        )
        return job_id

    def create_evaluation_session(self, **_: Any) -> str:
        self.session_calls += 1
        return "sess_1"

    def create_evaluation_item(self, **_: Any) -> str:
        self.item_calls += 1
        return "item_1"


class _RepoDouble:
    def __init__(self) -> None:
        self.trial_records: List[Dict[str, Any]] = []

    def get_treatment(self, _: str) -> Dict[str, Any]:
        return {
            "drummer_slug": "john_bonham",
            "status": "active",
            "cfg_overrides": {},
            "profile_overrides": {},
        }

    def create_trial_record(self, payload: Dict[str, Any]) -> str:
        self.trial_records.append(dict(payload))
        return str(payload["trial_id"])


class _EngineFallbackDouble:
    def generate_neutral(self, **_: Any) -> ProductionCandidate:
        return ProductionCandidate(
            role="neutral",
            event_stream=[{"time_sec": 0.0}],
            performance_spec=None,
            tempo_bpm=110.0,
            time_signature={"display": "4/4", "numerator": 4, "denominator": 4},
            bars=8,
            kit_id="default_kit",
            base_groove_path="tests/assets/base_groove.json",
            metadata={"engine": "neutral", "base_pattern_hash": "base_hash", "event_stream_hash": "neutral_hash"},
        )

    def generate_candidate(self, *, role: str, **_: Any) -> ProductionCandidate:
        return ProductionCandidate(
            role=role,
            event_stream=[{"time_sec": 0.0}, {"time_sec": 0.5}],
            performance_spec={"phrases": [{"id": "p1"}]},
            tempo_bpm=110.0,
            time_signature={"display": "4/4", "numerator": 4, "denominator": 4},
            bars=8,
            kit_id="default_kit",
            base_groove_path="tests/assets/base_groove.json",
            metadata={
                "engine": "production_performance_spec_v2",
                "production_engine_mode": "http",
                "production_metadata": {"backend": "fallback", "version": "1.2.0"},
                "base_pattern_hash": "base_hash",
                "event_stream_hash": f"{role}_hash",
                "paired_seed": 123,
                "profile_snapshot_hash": "profile_hash",
            },
            profile_snapshot={"drummer": "john_bonham"},
        )


class _EngineUnavailableDouble(_EngineFallbackDouble):
    def generate_candidate(self, **_: Any) -> ProductionCandidate:
        raise ProductionGenerationUnavailable("HTTP 503 from upstream")


class _EngineCanonicalDouble(_EngineFallbackDouble):
    def generate_candidate(self, *, role: str, treatment_id: Optional[str] = None, **_: Any) -> ProductionCandidate:
        meta = {
            "engine": "production_performance_spec_v2",
            "production_engine_mode": "http",
            "production_metadata": {"backend": "canonical_generation_api", "version": "1.2.0"},
            "base_pattern_hash": "base_hash",
            "event_stream_hash": f"{role}_hash_distinct",
            "paired_seed": 123,
            "profile_snapshot_hash": "profile_hash",
            "rollup_version": "phase5_v1",
        }
        return ProductionCandidate(
            role=role,
            event_stream=[{"time_sec": 0.0}, {"time_sec": 0.5}],
            performance_spec={"phrases": [{"id": "p1"}]},
            tempo_bpm=110.0,
            time_signature={"display": "4/4", "numerator": 4, "denominator": 4},
            bars=8,
            kit_id="default_kit",
            base_groove_path="tests/assets/base_groove.json",
            metadata={**meta, "treatment_id": treatment_id},
            profile_snapshot={"drummer": "john_bonham"},
        )


def _trial_input() -> TrialCreateInput:
    return TrialCreateInput(
        reviewer_id="test_reviewer_account",
        drummer_slug="john_bonham",
        base_groove_id="tests/assets/base_groove.json",
        challenger_treatment_id="trt_1",
        paired_seed=123,
        assignment_seed=456,
        repeats=4,
        render_profile_id="calibration_standard_v2",
        sample_pack_version="default",
        kit_id="default_kit",
    )


def test_staging_rejects_remote_generation_unavailable_and_commits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("CALIBRATION_GENERATION_MODE", "http")
    db = _TrialDbDouble()
    repo = _RepoDouble()
    service = CalibrationTrialService(db, repository=repo, engine=_EngineUnavailableDouble())

    with pytest.raises(CalibrationDependencyError):
        service.create_trial(_trial_input())

    assert db.run_log_calls == 0
    assert db.session_calls == 0
    assert db.item_calls == 0
    assert db.render_jobs == []
    assert repo.trial_records == []


def test_staging_rejects_fallback_backend_and_commits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("CALIBRATION_GENERATION_MODE", "http")
    db = _TrialDbDouble()
    repo = _RepoDouble()
    service = CalibrationTrialService(db, repository=repo, engine=_EngineFallbackDouble())

    with pytest.raises(CalibrationDependencyError):
        service.create_trial(_trial_input())

    assert db.run_log_calls == 0
    assert db.session_calls == 0
    assert db.item_calls == 0
    assert db.render_jobs == []
    assert repo.trial_records == []


def test_successful_trial_inserts_exactly_three_render_jobs_for_run_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CALIBRATION_GENERATION_MODE", "http")
    monkeypatch.delenv("CALIBRATION_RENDER_WORKER_DB_FINGERPRINT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example:5432/calibration")
    db = _TrialDbDouble()
    repo = _RepoDouble()
    service = CalibrationTrialService(db, repository=repo, engine=_EngineCanonicalDouble())

    result = service.create_trial(_trial_input())

    assert result["status"] == "queued"
    run_ids = set(result["run_ids"].values())
    assert len(db.render_jobs) == 3
    assert {item["run_id"] for item in db.render_jobs} == run_ids
    assert len(repo.trial_records) == 1


def test_http_error_maps_dependency_error_to_502() -> None:
    err = _http_error(CalibrationDependencyError("upstream unavailable"))
    assert err.status_code == 502


class _RenderDbDouble:
    def __init__(self) -> None:
        self.run = _FakeRun(run_id="calv2_run", drummer_slug="john_bonham", metadata={})
        self.logged_run_meta: List[Dict[str, Any]] = []
        self.jobs: List[Dict[str, Any]] = []
        self.audio_artifact_calls = 0

    def get_calibration_run(self, *, run_id: str) -> _FakeRun:
        assert run_id == self.run.run_id
        return self.run

    def log_calibration_render_job(self, **kwargs: Any) -> str:
        self.jobs.append(dict(kwargs))
        return "rjob_123"

    def log_calibration_run(self, *, metadata: Dict[str, Any], **_: Any) -> str:
        self.logged_run_meta.append(dict(metadata))
        self.run.metadata = dict(metadata)
        return self.run.run_id

    def log_audio_artifact(self, **_: Any) -> Optional[str]:
        self.audio_artifact_calls += 1
        return None


def test_render_service_is_queue_only_and_does_not_execute_worker_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALIBRATION_RENDER_WORKER_COMMAND", "echo SHOULD_NOT_RUN")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example:5432/calibration")
    monkeypatch.delenv("CALIBRATION_RENDER_WORKER_DB_FINGERPRINT", raising=False)

    db = _RenderDbDouble()
    svc = CalibrationRenderService(db)
    svc.render_run(
        RenderRequest(
            run_id="calv2_run",
            render_profile_id="calibration_standard_v2",
            sample_pack_version="default",
            kit_id="default_kit",
            seed=42,
            render_recipe={"schema": "calibration_render_recipe_v2"},
        )
    )

    assert len(db.jobs) == 1
    assert db.audio_artifact_calls == 0
    assert db.logged_run_meta[-1]["render"]["status"] == "queued"
    assert db.logged_run_meta[-1]["render"]["job_id"] == "rjob_123"


def test_render_service_detects_database_fingerprint_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.example:5432/calibration")
    monkeypatch.setenv("CALIBRATION_RENDER_WORKER_DB_FINGERPRINT", "definitely_mismatch")
    db = _RenderDbDouble()
    svc = CalibrationRenderService(db)

    with pytest.raises(RuntimeError, match="Database fingerprint mismatch"):
        svc.render_run(
            RenderRequest(
                run_id="calv2_run",
                render_profile_id="calibration_standard_v2",
                sample_pack_version="default",
                kit_id="default_kit",
                seed=42,
                render_recipe={"schema": "calibration_render_recipe_v2"},
            )
        )


class _WorkerDbDouble:
    def __init__(self) -> None:
        now = datetime.utcnow()
        self.job = CalibrationRenderJob(
            job_id="rjob_1",
            run_id="calv2_run",
            render_profile_id="calibration_standard_v2",
            sample_pack_version="default",
            status="queued",
            artifact_ids=[],
            error_text=None,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        self.claimed = False
        self.updates: List[Dict[str, Any]] = []
        self.logged_artifacts: List[Dict[str, Any]] = []
        self.run = _FakeRun(run_id="calv2_run", drummer_slug="john_bonham", metadata={"render": {"request": {"schema": "v2"}}})

    def list_calibration_render_jobs(self, *, statuses: Optional[List[str]] = None, limit: int = 25) -> List[CalibrationRenderJob]:
        if self.claimed:
            return []
        assert statuses is not None
        assert limit >= 1
        return [self.job]

    def claim_calibration_render_job(self, *, job_id: str, from_statuses: Optional[List[str]] = None) -> bool:
        assert job_id == "rjob_1"
        self.claimed = True
        return True

    def get_calibration_run(self, *, run_id: str) -> _FakeRun:
        assert run_id == "calv2_run"
        return self.run

    def get_calibration_run_events_payload(self, *, run_id: str) -> Dict[str, Any]:
        assert run_id == "calv2_run"
        return {"run_id": run_id, "event_stream": [{"time_sec": 0.0}]}

    def log_audio_artifact(self, **kwargs: Any) -> str:
        self.logged_artifacts.append(dict(kwargs))
        return "art_1"

    def update_calibration_render_job_status(self, **kwargs: Any) -> bool:
        self.updates.append(dict(kwargs))
        return True

    def log_calibration_run(self, *, metadata: Dict[str, Any], **_: Any) -> str:
        self.run.metadata = dict(metadata)
        return self.run.run_id


def test_only_worker_executes_command_and_writes_artifact_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db = _WorkerDbDouble()
    worker = CalibrationRenderWorker(
        db,
        command_template="renderer --input {input} --output {output}",
        max_retries=1,
        poll_interval_sec=0.01,
        command_timeout_sec=30,
    )

    called = {"count": 0}

    def _fake_subprocess_run(command: List[str], **_: Any) -> Any:
        called["count"] += 1
        out_index = command.index("--output") + 1
        out_path = Path(command[out_index])
        payload = {
            "artifacts": [
                {
                    "artifact_id": "art_1",
                    "artifact_type": "audio",
                    "storage_uri": str(tmp_path / "out.wav"),
                    "duration_sec": 4.0,
                }
            ]
        }
        out_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("backend.workers.calibration_render_worker.subprocess.run", _fake_subprocess_run)

    summary = worker.run_once(max_jobs=1)

    assert called["count"] == 1
    assert summary["completed"] == 1
    completed_updates = [u for u in db.updates if u.get("status") == "completed"]
    assert len(completed_updates) == 1
    assert completed_updates[0]["artifact_ids"] == ["art_1"]


def test_refresh_ready_query_requires_artifacts_for_neutral_and_both_visible_lanes() -> None:
    captured: Dict[str, Any] = {}

    class _Result:
        rowcount = 1

    class _Conn:
        def execute(self, stmt: Any, params: Dict[str, Any]) -> _Result:
            captured["sql"] = str(stmt)
            captured["params"] = dict(params)
            return _Result()

    class _BeginCtx:
        def __enter__(self) -> _Conn:
            return _Conn()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _Engine:
        def begin(self) -> _BeginCtx:
            return _BeginCtx()

    class _Db:
        _engine = _Engine()

        @staticmethod
        def get_drummer(_: str) -> Dict[str, Any]:
            return {"display_name": "John Bonham"}

    repo = CalibrationV2Repository(_Db())
    changed = repo.refresh_ready_trials_for_reviewer("test_reviewer_account")
    assert changed == 1
    sql = captured["sql"]
    assert "EXISTS (SELECT 1 FROM public.audio_artifacts a WHERE a.run_id = t.neutral_run_id)" in sql
    assert "EXISTS (SELECT 1 FROM public.audio_artifacts a WHERE a.run_id = t.visible_a_run_id)" in sql
    assert "EXISTS (SELECT 1 FROM public.audio_artifacts a WHERE a.run_id = t.visible_b_run_id)" in sql

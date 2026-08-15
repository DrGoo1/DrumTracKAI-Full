from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from backend import calibration_v2_api
from backend.services.calibration_trial_readiness import (
    CalibrationTrialReadinessService,
    ReadinessRefreshResult,
)


class _Result:
    def __init__(self, rowcount: int = 0, mapping: Dict[str, Any] | None = None) -> None:
        self.rowcount = rowcount
        self._mapping = mapping

    def mappings(self):
        return self

    def first(self):
        return self._mapping


class _Connection:
    def __init__(self, calls: List[Dict[str, Any]]) -> None:
        self.calls = calls

    def execute(self, statement: Any, params: Dict[str, Any]):
        sql = str(statement)
        self.calls.append({"sql": sql, "params": dict(params)})
        if sql.lstrip().upper().startswith("SELECT"):
            return _Result(
                mapping={
                    "trial_id": params.get("trial_id"),
                    "status": "ready",
                    "control_backend": "onnx",
                    "challenger_backend": "onnx",
                    "populated_event_streams": 3,
                    "completed_jobs": 3,
                    "durable_artifacts": 3,
                    "strict_ready": True,
                }
            )
        if "t.status = 'ready'" in sql:
            return _Result(rowcount=1)
        return _Result(rowcount=2)


class _Context:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def begin(self) -> _Context:
        return _Context(_Connection(self.calls))

    def connect(self) -> _Context:
        return _Context(_Connection(self.calls))


class _Db:
    def __init__(self) -> None:
        self._engine = _Engine()


class _ReadinessDouble:
    def __init__(self) -> None:
        self.reviewers: List[str] = []

    def refresh_for_reviewer(self, reviewer_id: str) -> ReadinessRefreshResult:
        self.reviewers.append(reviewer_id)
        return ReadinessRefreshResult(ready_count=1, invalidated_count=0)


class _ReviewerRepo:
    def __init__(self, row: Dict[str, Any] | None = None) -> None:
        self.row = row
        self.started: List[str] = []

    def list_ready_drummers(self, reviewer_id: str):
        assert reviewer_id == "reviewer-1"
        return [{"drummer_slug": "john_bonham", "display_name": "John Bonham", "ready_trial_count": 1}]

    def next_ready_item_for_reviewer(self, *, reviewer_id: str, target_drummer_slug=None):
        assert reviewer_id == "reviewer-1"
        return self.row

    def require_item_ownership(self, *, item_id: str, reviewer_id: str):
        assert item_id == "item-1"
        assert reviewer_id == "reviewer-1"
        assert self.row is not None
        return self.row

    def mark_session_started(self, session_id: str) -> None:
        self.started.append(session_id)

    @staticmethod
    def drummer_display_name(_slug: str) -> str:
        return "John Bonham"


class _ArtifactDb:
    @staticmethod
    def get_audio_artifacts_for_run(*, run_id: str):
        return []


def _reviewer_context():
    return SimpleNamespace(reviewer_id="reviewer-1")


def _item_row(status_value: str = "ready") -> Dict[str, Any]:
    return {
        "item_id": "item-1",
        "session_id": "session-1",
        "trial_id": "trial-1",
        "base_groove_id": "base",
        "target_drummer_slug": "john_bonham",
        "baseline_run_id": "neutral",
        "candidate_a_run_id": "run-a",
        "candidate_b_run_id": "run-b",
        "trial_status": status_value,
    }


def test_strict_readiness_refresh_uses_all_three_evidence_layers() -> None:
    db = _Db()
    service = CalibrationTrialReadinessService(db)

    result = service.refresh_for_reviewer("reviewer-1")

    assert result.invalidated_count == 1
    assert result.ready_count == 2
    assert len(db._engine.calls) == 2
    combined = "\n".join(call["sql"] for call in db._engine.calls)
    assert "generation_metadata_json" in combined
    assert "control,production_metadata,backend" in combined
    assert "challenger,production_metadata,backend" in combined
    assert "calibration_run_events" in combined
    assert "calibration_render_jobs" in combined
    assert "audio_artifacts" in combined
    assert "storage_uri LIKE 's3://%'" in combined
    assert "diagnostic_only" in combined
    assert "NOT LIKE '%procedural%'" in combined
    assert "BTRIM(e.event_stream_json::text)" in combined
    assert "BTRIM(j.artifact_ids_json::text)" in combined
    assert "BTRIM(a.render_recipe_json::text)" in combined


def test_readiness_diagnostics_returns_strict_summary() -> None:
    db = _Db()
    service = CalibrationTrialReadinessService(db)

    result = service.diagnostics_for_trial("trial-1")

    assert result["strict_ready"] is True
    assert result["populated_event_streams"] == 3
    assert result["completed_jobs"] == 3
    assert result["durable_artifacts"] == 3


def test_reviewer_drummers_refreshes_strict_readiness_before_listing() -> None:
    readiness = _ReadinessDouble()
    repo = _ReviewerRepo()

    result = calibration_v2_api.reviewer_drummers(
        context=_reviewer_context(),
        repo=repo,
        readiness=readiness,
    )

    assert readiness.reviewers == ["reviewer-1"]
    assert result["items"][0]["drummer_slug"] == "john_bonham"


def test_reviewer_next_refreshes_strict_readiness_before_returning_item() -> None:
    readiness = _ReadinessDouble()
    repo = _ReviewerRepo(_item_row())

    result = calibration_v2_api.reviewer_next(
        target_drummer_slug="john_bonham",
        context=_reviewer_context(),
        repo=repo,
        readiness=readiness,
        db=_ArtifactDb(),
    )

    assert readiness.reviewers == ["reviewer-1"]
    assert result["item"]["trial_id"] == "trial-1"
    assert repo.started == ["session-1"]


def test_direct_reviewer_item_rejects_trial_that_is_not_strict_ready() -> None:
    readiness = _ReadinessDouble()
    repo = _ReviewerRepo(_item_row("queued"))

    with pytest.raises(HTTPException) as error:
        calibration_v2_api.reviewer_item(
            item_id="item-1",
            context=_reviewer_context(),
            repo=repo,
            readiness=readiness,
            db=_ArtifactDb(),
        )

    assert error.value.status_code == 409
    assert repo.started == []

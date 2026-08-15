from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from backend import calibration_v2_api
from backend.services.calibration_assimilation_service import (
    AssimilationModelStatus,
    CalibrationAssimilationService,
    CalibrationProvisioningUnavailable,
)
from backend.services.calibration_profile_resolver import ResolvedCalibrationProfile


class _Db:
    _engine = object()

    @staticmethod
    def _compute_assimilation_score(**_: Any) -> int:
        return 82


class _Resolver:
    def resolve(self, *, drummer_slug: str, **_: Any) -> ResolvedCalibrationProfile:
        return ResolvedCalibrationProfile(
            drummer_slug=drummer_slug,
            profile={
                "drummer_id": drummer_slug,
                "preset_id": f"phase6_{drummer_slug}",
                "pocket_tightness": 0.78,
                "humanness": 0.69,
                "instrument_timing_profiles": [{"instrument": "snare"}],
                "instrument_dynamic_profiles": [{"instrument": "snare"}],
                "fill_behavior": [{"fill_probability": 0.4}],
                "phrase_features": [{"phrase_length_bars": 4}],
                "cymbal_language": {"ride_probability": 0.5},
                "limb_coordination": {"kick_snare": 0.7},
                "phase32_42_features": {
                    "phase37_42": {
                        "drummer_personality_profile": {"aggressiveness": 0.8}
                    }
                },
            },
            snapshot_hash="profile-hash",
            rollup_version="phase5_v2",
            source_counts={
                "timing_profiles": 1,
                "dynamic_profiles": 1,
                "fill_profiles": 1,
                "phrase_features": 1,
                "phase32_42_payloads": 1,
            },
        )


class _Repo:
    def __init__(self) -> None:
        self.created_treatments: List[Dict[str, Any]] = []

    @staticmethod
    def drummer_display_name(slug: str) -> str:
        return "John Bonham" if slug == "john_bonham" else slug

    def create_treatment(self, **kwargs: Any) -> str:
        self.created_treatments.append(dict(kwargs))
        return "trt_phase6"


class _TrialService:
    def __init__(self) -> None:
        self.inputs = []

    def create_trial(self, request: Any) -> Dict[str, Any]:
        self.inputs.append(request)
        return {
            "status": "queued",
            "trial_id": "trial_new",
            "item_id": "item_new",
            "session_id": "session_new",
        }


class _Service(CalibrationAssimilationService):
    def __init__(self) -> None:
        self.repo = _Repo()
        self.trials = _TrialService()
        super().__init__(
            _Db(),
            repository=self.repo,
            resolver=_Resolver(),
            trial_service=self.trials,
        )
        self.active_treatment: Optional[Dict[str, Any]] = {
            "treatment_id": "trt_active",
            "drummer_slug": "john_bonham",
            "cfg_overrides": {"humanizeAmount": 0.22},
        }
        self.open_trial: Optional[Dict[str, Any]] = None

    def _rollup_slugs(self) -> List[str]:
        return ["john_bonham"]

    def _analysis_evidence(self, drummer_slug: str) -> Dict[str, int]:
        assert drummer_slug == "john_bonham"
        return {
            "source_song_count": 12,
            "source_artifact_count": 12,
            "source_stem_count": 72,
            "hit_event_count": 24000,
            "fill_event_count": 180,
            "technique_event_count": 90,
        }

    def _active_treatment(self, drummer_slug: str) -> Optional[Dict[str, Any]]:
        assert drummer_slug == "john_bonham"
        return dict(self.active_treatment) if self.active_treatment else None

    def _trial_counts(self, *, reviewer_id: Optional[str], drummer_slug: str) -> Dict[str, int]:
        assert drummer_slug == "john_bonham"
        return {"ready_trial_count": 1 if reviewer_id else 0, "queued_trial_count": 0}

    def _open_trial(self, *, reviewer_id: str, drummer_slug: str) -> Optional[Dict[str, Any]]:
        return dict(self.open_trial) if self.open_trial else None

    def _load_phase6_preset(self, drummer_slug: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "preset_id": f"phase6_{drummer_slug}",
            "name": "Bonham Phase 6",
            "deltas": {
                "humanizeAmount": 0.22,
                "ghostNoteAmount": 0.31,
                "swingAmount": 0.15,
                "fillDensity": 0.42,
            },
            "policies": {"source": "phase6"},
        }


def test_assimilated_model_catalog_reports_signature_song_evidence() -> None:
    service = _Service()

    models = service.list_models(reviewer_id="reviewer-1")

    assert len(models) == 1
    model = models[0]
    assert model.drummer_slug == "john_bonham"
    assert model.model_ready is True
    assert model.can_queue_trial is True
    assert model.source_song_count == 12
    assert model.hit_event_count == 24000
    assert model.profile_snapshot_hash == "profile-hash"
    assert model.assimilation_score == 82
    assert model.profile_sections["personality"] is True
    assert model.reviewer_payload()["ready_trial_count"] == 1


def test_phase6_preset_bootstraps_active_calibration_treatment() -> None:
    service = _Service()
    service.active_treatment = None

    result = service.bootstrap_phase6_treatment(
        drummer_slug="john_bonham",
        created_by="00000000-0000-0000-0000-000000000001",
    )

    assert result["created"] is True
    assert len(service.repo.created_treatments) == 1
    created = service.repo.created_treatments[0]
    assert created["drummer_slug"] == "john_bonham"
    assert created["status_value"] == "active"
    assert created["cfg_overrides"]["humanizeAmount"] == 0.22
    assert created["cfg_overrides"]["fillDensity"] == 0.42
    assert created["profile_overrides"] == {}


def test_existing_open_trial_is_reused_instead_of_duplicated() -> None:
    service = _Service()
    service.open_trial = {
        "status": "queued",
        "trial_id": "trial_existing",
        "item_id": "item_existing",
        "session_id": "session_existing",
    }

    result = service.ensure_reviewer_trial(
        reviewer_id="reviewer-1",
        drummer_slug="john_bonham",
    )

    assert result["trial_id"] == "trial_existing"
    assert result["created"] is False
    assert service.trials.inputs == []


def test_review_trial_uses_active_phase6_treatment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALIBRATION_DEFAULT_BASE_GROOVE_ID", "base_groove")
    monkeypatch.setenv("CALIBRATION_DEFAULT_REPEATS", "4")
    monkeypatch.setenv("CALIBRATION_SAMPLE_PACK_VERSION", "neutral_reference_kit_v1")
    monkeypatch.setenv("CALIBRATION_KIT_ID", "neutral_reference_kit_v1")
    service = _Service()

    result = service.ensure_reviewer_trial(
        reviewer_id="reviewer-1",
        drummer_slug="john_bonham",
    )

    assert result["created"] is True
    assert result["trial_id"] == "trial_new"
    assert len(service.trials.inputs) == 1
    request = service.trials.inputs[0]
    assert request.challenger_treatment_id == "trt_active"
    assert request.base_groove_id == "base_groove"
    assert request.sample_pack_version == "neutral_reference_kit_v1"


def test_missing_phase6_treatment_blocks_trial_queueing() -> None:
    service = _Service()
    service.active_treatment = None

    with pytest.raises(CalibrationProvisioningUnavailable, match="Phase 6 treatment bootstrap"):
        service.ensure_reviewer_trial(
            reviewer_id="reviewer-1",
            drummer_slug="john_bonham",
        )


class _Readiness:
    def __init__(self) -> None:
        self.reviewers: List[str] = []

    def refresh_for_reviewer(self, reviewer_id: str):
        self.reviewers.append(reviewer_id)
        return SimpleNamespace(ready_count=0, invalidated_count=0)


class _AssimilationApiDouble:
    def __init__(self) -> None:
        self.queued = []
        self.model = AssimilationModelStatus(
            drummer_slug="john_bonham",
            display_name="John Bonham",
            profile_resolved=True,
            model_ready=True,
            can_queue_trial=True,
            rollup_version="phase5_v2",
            profile_snapshot_hash="profile-hash",
            source_counts={"timing_profiles": 1},
            source_song_count=12,
            source_stem_count=72,
            hit_event_count=24000,
            fill_event_count=180,
            technique_event_count=90,
            assimilation_score=82,
            profile_sections={"microtiming": True, "personality": True},
            active_treatment_id="trt_active",
            ready_trial_count=0,
            queued_trial_count=0,
            blockers=[],
        )

    def list_models(self, **_: Any):
        return [self.model]

    def ensure_reviewer_trial(self, *, reviewer_id: str, drummer_slug: str):
        self.queued.append((reviewer_id, drummer_slug))
        return {"status": "queued", "trial_id": "trial_queued"}


class _RepoApiDouble:
    def next_ready_item_for_reviewer(self, **_: Any):
        return None


def test_reviewer_catalog_comes_from_assimilated_models() -> None:
    readiness = _Readiness()
    assimilation = _AssimilationApiDouble()

    response = calibration_v2_api.reviewer_drummers(
        context=SimpleNamespace(reviewer_id="reviewer-1"),
        readiness=readiness,
        assimilation=assimilation,
    )

    assert readiness.reviewers == ["reviewer-1"]
    assert response["items"][0]["drummer_slug"] == "john_bonham"
    assert response["items"][0]["source_song_count"] == 12


def test_reviewer_next_auto_queues_assimilation_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALIBRATION_AUTO_QUEUE_REVIEW_TRIALS", "true")
    readiness = _Readiness()
    assimilation = _AssimilationApiDouble()

    response = calibration_v2_api.reviewer_next(
        target_drummer_slug="john_bonham",
        context=SimpleNamespace(reviewer_id="reviewer-1"),
        repo=_RepoApiDouble(),
        readiness=readiness,
        assimilation=assimilation,
        db=SimpleNamespace(),
    )

    assert response["item"] is None
    assert response["status"] == "preparing"
    assert response["trial_id"] == "trial_queued"
    assert assimilation.queued == [("reviewer-1", "john_bonham")]

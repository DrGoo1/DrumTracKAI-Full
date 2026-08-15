from __future__ import annotations

from types import SimpleNamespace

from backend import calibration_v2_api


class FakeRepo:
    @staticmethod
    def drummer_display_name(_slug: str) -> str:
        return "Target Drummer"


class FakeDb:
    @staticmethod
    def get_audio_artifacts_for_run(*, run_id: str):
        return [
            SimpleNamespace(
                artifact_id=f"artifact_{run_id}",
                artifact_type="candidate_audio",
                storage_uri=f"https://audio.example.test/{run_id}.wav",
                duration_sec=8.5,
                loudness_lufs=-16.0,
                sample_pack_version="pilot_kit_v1",
            )
        ]


def _row():
    return {
        "item_id": "item_1",
        "session_id": "session_1",
        "trial_id": "trial_1",
        "target_drummer_slug": "target",
        "base_groove_id": "base",
        "baseline_run_id": "neutral",
        "candidate_a_run_id": "run_a",
        "candidate_b_run_id": "run_b",
        "assignment_json": {"A": {"role": "challenger"}},
        "ab_mapping_json": {"A": {"role": "challenger"}},
        "challenger_treatment_id": "treatment_secret",
        "control_profile_snapshot_json": {"secret": True},
    }


def test_reviewer_payload_does_not_expose_hidden_mapping():
    payload = calibration_v2_api._reviewer_item_payload(
        row=_row(),
        repo=FakeRepo(),
        db=FakeDb(),
    )
    encoded = repr(payload)
    for forbidden in (
        "assignment_json",
        "ab_mapping_json",
        "challenger_treatment_id",
        "control_profile_snapshot",
        "treatment_secret",
        "challenger",
    ):
        assert forbidden not in encoded
    assert set(payload["lanes"]) == {"neutral", "A", "B"}


def test_reviewer_payload_matches_frontend_contract():
    payload = calibration_v2_api._reviewer_item_payload(
        row=_row(),
        repo=FakeRepo(),
        db=FakeDb(),
    )

    assert payload["item_id"] == "item_1"
    assert payload["session_id"] == "session_1"
    assert payload["trial_id"] == "trial_1"
    assert payload["base_groove_id"] == "base"
    assert payload["target_drummer_display_name"] == "Target Drummer"

    artifact = payload["lanes"]["A"][0]
    assert artifact == {
        "artifact_id": "artifact_run_a",
        "artifact_type": "candidate_audio",
        "url": "https://audio.example.test/run_a.wav",
        "duration_sec": 8.5,
        "loudness_lufs": -16.0,
        "sample_pack_version": "pilot_kit_v1",
    }

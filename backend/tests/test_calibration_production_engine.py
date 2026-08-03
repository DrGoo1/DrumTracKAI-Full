from __future__ import annotations

import json
from pathlib import Path

from backend.services.calibration_production_engine import CalibrationProductionEngine
from backend.services.calibration_profile_resolver import ResolvedCalibrationProfile
from backend.services.production_performance_client import PerformanceSpecResult


class FakeProfileResolver:
    def resolve(self, *, drummer_slug, profile_overrides=None, strict=True):
        profile = {"drummer_id": drummer_slug, "velocity_bias": 0, **(profile_overrides or {})}
        return ResolvedCalibrationProfile(
            drummer_slug=drummer_slug,
            profile=profile,
            snapshot_hash=f"profile-{profile['velocity_bias']}",
            rollup_version="test",
            source_counts={"timing_profiles": 1},
        )


class FakePerformanceClient:
    def generate_performance_spec(self, *, cfg, songmap_summary, drummer_profile):
        bias = int(drummer_profile.get("velocity_bias", 0))
        profiles = []
        for instrument, base in (("kick", 100), ("snare_center", 90), ("hihat_closed", 70)):
            profiles.append(
                {
                    "instrumentId": instrument,
                    "microTiming": {
                        "subdivisionOffsetsMs": [0.0, 2.0, -1.0, 1.0] * 4,
                        "swingAmount": 0.0,
                        "laidBackAmount": 0.0,
                    },
                    "velocityProfile": {
                        "base": base + bias,
                        "accentBoost": 10,
                        "ghostReduction": 0.5,
                        "randomRange": 8,
                        "phraseShape": "flat",
                    },
                    "ghostDensity": 0.0,
                    "flamProbability": 0.0,
                    "dragProbability": 0.0,
                }
            )
        return PerformanceSpecResult(
            spec={
                "styleId": "rock",
                "globalFeel": "straight",
                "quantizationBase": "16th",
                "phrases": [
                    {
                        "phraseId": "calibration",
                        "barStart": 0,
                        "barEnd": int(cfg["endMeasure"]),
                        "profiles": profiles,
                    }
                ],
            },
            metadata={"model_version": "test"},
            endpoint="fake",
            engine_mode="fake",
        )


def write_pattern(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "tempo_bpm": 120,
                "time_signature": "4/4",
                "ppqn": 960,
                "pattern_events": [
                    {
                        "barIndex": 0,
                        "barStartTime": 0.0,
                        "barEndTime": 2.0,
                        "bar_pos_frac": 0.0,
                        "time_sec": 0.0,
                        "instrument_id": "kick",
                        "velocity": 100,
                    },
                    {
                        "barIndex": 0,
                        "barStartTime": 0.0,
                        "barEndTime": 2.0,
                        "bar_pos_frac": 0.25,
                        "time_sec": 0.5,
                        "instrument_id": "snare_center",
                        "velocity": 95,
                    },
                    {
                        "barIndex": 0,
                        "barStartTime": 0.0,
                        "barEndTime": 2.0,
                        "bar_pos_frac": 0.5,
                        "time_sec": 1.0,
                        "instrument_id": "hihat_closed",
                        "velocity": 75,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_same_seed_is_deterministic(monkeypatch, tmp_path):
    pattern = tmp_path / "base.json"
    write_pattern(pattern)
    monkeypatch.setattr(
        "backend.services.calibration_production_engine._resolve_base_groove_path",
        lambda _value: pattern,
    )
    engine = CalibrationProductionEngine(
        object(),
        performance_client=FakePerformanceClient(),
        profile_resolver=FakeProfileResolver(),
    )
    first = engine.generate_candidate(
        role="control",
        base_groove_id="base",
        drummer_slug="john_bonham",
        seed=1234,
        repeats=2,
    )
    second = engine.generate_candidate(
        role="control",
        base_groove_id="base",
        drummer_slug="john_bonham",
        seed=1234,
        repeats=2,
    )
    assert first.metadata["event_stream_hash"] == second.metadata["event_stream_hash"]
    assert first.event_stream == second.event_stream


def test_treatment_changes_profile_but_not_base_pattern(monkeypatch, tmp_path):
    pattern = tmp_path / "base.json"
    write_pattern(pattern)
    monkeypatch.setattr(
        "backend.services.calibration_production_engine._resolve_base_groove_path",
        lambda _value: pattern,
    )
    engine = CalibrationProductionEngine(
        object(),
        performance_client=FakePerformanceClient(),
        profile_resolver=FakeProfileResolver(),
    )
    control = engine.generate_candidate(
        role="control",
        base_groove_id="base",
        drummer_slug="john_bonham",
        seed=55,
        repeats=1,
    )
    challenger = engine.generate_candidate(
        role="challenger",
        base_groove_id="base",
        drummer_slug="john_bonham",
        seed=55,
        repeats=1,
        profile_overrides={"velocity_bias": 12},
        treatment_id="trt_test",
    )
    assert control.metadata["base_pattern_hash"] == challenger.metadata["base_pattern_hash"]
    assert control.metadata["paired_seed"] == challenger.metadata["paired_seed"]
    assert control.metadata["profile_snapshot_hash"] != challenger.metadata["profile_snapshot_hash"]
    assert control.metadata["event_stream_hash"] != challenger.metadata["event_stream_hash"]

from __future__ import annotations

from array import array
import importlib.util
import json
from pathlib import Path
import wave

import pytest


def _load_renderer_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "calibration_sample_renderer.py"
    spec = importlib.util.spec_from_file_location("calibration_sample_renderer", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_sample(path: Path, *, sample_rate: int = 48000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = array("h")
    for index in range(sample_rate // 20):
        value = int(18000 * max(0.0, 1.0 - index / float(sample_rate // 20)))
        frames.append(value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames.tobytes())


def _manifest(tmp_path: Path) -> Path:
    sample_path = tmp_path / "samples" / "kick.wav"
    _write_sample(sample_path)
    manifest = {
        "kitId": "pilot_kit",
        "name": "Pilot Kit",
        "version": "calibration_pilot_kit_v1",
        "mics": [{"id": "close", "label": "Close", "defaultGainDb": 0.0}],
        "articulations": {
            "kick": {
                "mics": {
                    "close": {
                        "velocityLayers": [
                            {
                                "min": 1,
                                "max": 127,
                                "roundRobin": ["samples/kick.wav"],
                            }
                        ]
                    }
                }
            }
        },
        "mixDefaults": {"masterGainDb": -3.0},
    }
    path = tmp_path / "kit_manifest_v1.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _payload() -> dict:
    return {
        "job": {
            "job_id": "rjob_1",
            "run_id": "trial_1_control",
            "render_profile_id": "calibration_standard_v2",
            "sample_pack_version": "calibration_pilot_kit_v1",
            "attempt": 1,
        },
        "run": {
            "run_id": "trial_1_control",
            "drummer_slug": "john_bonham",
            "metadata": {},
        },
        "render_request": {
            "seed": 123,
            "render_recipe": {
                "trial_id": "trial_1",
                "role": "control",
            },
        },
        "run_events": {
            "tempo_bpm": 110.0,
            "event_stream": [
                {
                    "time_sec": 0.0,
                    "instrument_id": "kick",
                    "velocity": 110,
                },
                {
                    "time_sec": 0.5,
                    "instrument_id": "kick",
                    "velocity": 90,
                },
            ],
        },
    }


def test_real_sample_renderer_creates_deterministic_non_diagnostic_wav(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_renderer_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CALIBRATION_SAMPLE_MANIFEST_URI", str(manifest_path))
    monkeypatch.setenv("CALIBRATION_SAMPLE_PACK_VERSION", "calibration_pilot_kit_v1")
    monkeypatch.setenv("CALIBRATION_RENDERER_VERSION", "drumtrackai_sample_renderer_v1")
    monkeypatch.setenv("CALIBRATION_RENDER_UPLOAD_ENABLED", "false")

    first = renderer.render_request(_payload(), working_dir=tmp_path / "first")
    second = renderer.render_request(_payload(), working_dir=tmp_path / "second")

    first_artifact = first["artifacts"][0]
    second_artifact = second["artifacts"][0]
    first_path = Path(first_artifact["storage_uri"])
    second_path = Path(second_artifact["storage_uri"])

    assert first_path.is_file()
    assert second_path.is_file()
    assert first_artifact["render_recipe"]["diagnostic_only"] is False
    assert first_artifact["render_recipe"]["renderer_version"] == "drumtrackai_sample_renderer_v1"
    assert first_artifact["sample_pack_version"] == "calibration_pilot_kit_v1"
    assert first_artifact["render_recipe"]["event_count"] == 2
    assert first_artifact["render_recipe"]["sha256"] == second_artifact["render_recipe"]["sha256"]
    assert first_artifact["duration_sec"] > 0.5


def test_renderer_fails_when_manifest_has_no_required_articulation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_renderer_module()
    manifest_path = _manifest(tmp_path)
    payload = _payload()
    payload["run_events"]["event_stream"][0]["instrument_id"] = "ride_bell"
    payload["run_events"]["event_stream"] = payload["run_events"]["event_stream"][:1]

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CALIBRATION_SAMPLE_MANIFEST_URI", str(manifest_path))
    monkeypatch.setenv("CALIBRATION_SAMPLE_PACK_VERSION", "calibration_pilot_kit_v1")
    monkeypatch.setenv("CALIBRATION_RENDERER_VERSION", "drumtrackai_sample_renderer_v1")
    monkeypatch.setenv("CALIBRATION_RENDER_UPLOAD_ENABLED", "false")

    with pytest.raises(renderer.SampleRendererError, match="no articulation"):
        renderer.render_request(payload, working_dir=tmp_path / "render")


def test_strict_runtime_rejects_local_sample_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_renderer_module()
    manifest_path = _manifest(tmp_path)
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("CALIBRATION_SAMPLE_MANIFEST_URI", str(manifest_path))
    monkeypatch.setenv("CALIBRATION_SAMPLE_PACK_VERSION", "calibration_pilot_kit_v1")
    monkeypatch.setenv("CALIBRATION_RENDERER_VERSION", "drumtrackai_sample_renderer_v1")

    with pytest.raises(renderer.SampleRendererError, match="Local sample paths"):
        renderer.render_request(_payload(), working_dir=tmp_path / "render")


def test_renderer_requires_nonempty_event_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    renderer = _load_renderer_module()
    manifest_path = _manifest(tmp_path)
    payload = _payload()
    payload["run_events"]["event_stream"] = []

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("CALIBRATION_SAMPLE_MANIFEST_URI", str(manifest_path))
    monkeypatch.setenv("CALIBRATION_SAMPLE_PACK_VERSION", "calibration_pilot_kit_v1")
    monkeypatch.setenv("CALIBRATION_RENDERER_VERSION", "drumtrackai_sample_renderer_v1")
    monkeypatch.setenv("CALIBRATION_RENDER_UPLOAD_ENABLED", "false")

    with pytest.raises(renderer.SampleRendererError, match="No renderable events"):
        renderer.render_request(payload, working_dir=tmp_path / "render")

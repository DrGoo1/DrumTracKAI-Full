from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient


def _load_bootstrap_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap_generation_model.py"
    spec = importlib.util.spec_from_file_location("bootstrap_generation_model", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def llm_app_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("INFERENCE_BACKEND", "onnx")
    module = importlib.import_module("llm_service.app")
    return module


def test_readiness_valid_onnx_matching_sha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm_app_module) -> None:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"valid_onnx_payload")
    expected_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()

    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setenv("ONNX_MODEL_PATH", str(model_path))
    monkeypatch.setenv("GENERATION_MODEL_SHA256", expected_sha)
    monkeypatch.setenv("DRUMTRACKAI_MODEL_VERSION", "drum_humanizer_v1.1.17_onnx")

    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "onnx", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", object(), raising=False)
    monkeypatch.setattr(llm_app_module, "MODEL_LOAD_ERROR", None, raising=False)

    snapshot = llm_app_module._readiness_snapshot()

    assert snapshot["ready"] is True
    assert snapshot["backend"] == "onnx"
    assert snapshot["backend_ready"] is True
    assert snapshot["model_sha256_verified"] is True
    assert snapshot["model_version"] == "drum_humanizer_v1.1.17_onnx"


def test_readiness_valid_onnx_wrong_sha_not_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm_app_module) -> None:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"valid_onnx_payload")

    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setenv("ONNX_MODEL_PATH", str(model_path))
    monkeypatch.setenv("GENERATION_MODEL_SHA256", "0" * 64)

    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "onnx", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", object(), raising=False)
    monkeypatch.setattr(llm_app_module, "MODEL_LOAD_ERROR", None, raising=False)

    snapshot = llm_app_module._readiness_snapshot()

    assert snapshot["ready"] is False
    assert snapshot["backend_ready"] is False
    assert snapshot["reason"] == "ONNX model SHA-256 does not match GENERATION_MODEL_SHA256"


def test_readiness_missing_onnx_not_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm_app_module) -> None:
    model_path = tmp_path / "missing_model.onnx"

    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setenv("ONNX_MODEL_PATH", str(model_path))
    monkeypatch.setenv("GENERATION_MODEL_SHA256", "a" * 64)

    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "onnx", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", object(), raising=False)
    monkeypatch.setattr(llm_app_module, "MODEL_LOAD_ERROR", None, raising=False)

    snapshot = llm_app_module._readiness_snapshot()

    assert snapshot["ready"] is False
    assert snapshot["backend_ready"] is False
    assert snapshot["reason"] == "ONNX model path does not exist"


def test_readiness_fallback_backend_not_ready(monkeypatch: pytest.MonkeyPatch, llm_app_module) -> None:
    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.delenv("GENERATION_MODEL_SHA256", raising=False)

    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "fallback", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", None, raising=False)
    monkeypatch.setattr(llm_app_module, "MODEL_LOAD_ERROR", None, raising=False)

    snapshot = llm_app_module._readiness_snapshot()

    assert snapshot["ready"] is False
    assert snapshot["backend_ready"] is False
    assert snapshot["reason"] == "Strict readiness requires ACTIVE_BACKEND=onnx"


def test_exactly_one_healthz_route(llm_app_module) -> None:
    routes = [
        route
        for route in llm_app_module.app.routes
        if getattr(route, "path", None) == "/healthz"
        and "GET" in (getattr(route, "methods", set()) or set())
    ]
    assert len(routes) == 1


def test_exactly_one_readyz_route(llm_app_module) -> None:
    routes = [
        route
        for route in llm_app_module.app.routes
        if getattr(route, "path", None) == "/readyz"
        and "GET" in (getattr(route, "methods", set()) or set())
    ]
    assert len(routes) == 1


def test_strict_fallback_infer_one_cache_not_used(monkeypatch: pytest.MonkeyPatch, llm_app_module) -> None:
    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "fallback", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", None, raising=False)
    monkeypatch.setattr(llm_app_module, "CACHE_ENABLED", True, raising=False)

    called = {"count": 0}

    def _cached_infer(*_args, **_kwargs):
        called["count"] += 1
        return dict(llm_app_module.DEFAULT_HUMANIZATION_PARAMS)

    monkeypatch.setattr(llm_app_module, "_cached_infer", _cached_infer, raising=False)

    req = llm_app_module.HumanizationRequest(tempo_bpm=110.0, style="rock", pattern_complexity=0.5)
    with pytest.raises(llm_app_module.HTTPException) as exc:
        llm_app_module._infer_one(req)

    assert exc.value.status_code == 503
    assert called["count"] == 0


def test_strict_fallback_infer_many_cache_not_used(monkeypatch: pytest.MonkeyPatch, llm_app_module) -> None:
    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "fallback", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", None, raising=False)
    monkeypatch.setattr(llm_app_module, "CACHE_ENABLED", True, raising=False)

    called = {"count": 0}

    def _cached_infer(*_args, **_kwargs):
        called["count"] += 1
        return dict(llm_app_module.DEFAULT_HUMANIZATION_PARAMS)

    monkeypatch.setattr(llm_app_module, "_cached_infer", _cached_infer, raising=False)

    req = llm_app_module.HumanizationRequest(tempo_bpm=110.0, style="rock", pattern_complexity=0.5)
    with pytest.raises(llm_app_module.HTTPException) as exc:
        llm_app_module._infer_many([req])

    assert exc.value.status_code == 503
    assert called["count"] == 0


def test_strict_fallback_performance_spec_returns_503(monkeypatch: pytest.MonkeyPatch, llm_app_module) -> None:
    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "fallback", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", None, raising=False)
    monkeypatch.setattr(llm_app_module, "CACHE_ENABLED", True, raising=False)

    client = TestClient(llm_app_module.app)
    probe = {
        "cfg": {
            "sectionId": "calibration_probe",
            "startMeasure": 0,
            "endMeasure": 3,
            "tempos": [110, 110, 110, 110],
            "timeSignature": [4, 4],
            "style": "rock",
            "drummer": "john_bonham",
            "intensity": 0.65,
            "variation": 0.5,
            "complexity": 0.5,
            "humanizeAmount": 0.7,
            "ghostNoteAmount": 0.5,
            "swingAmount": 0.0,
        },
        "songmap_summary": {
            "styleGroup": "rock",
            "sections": [
                {
                    "label": "calibration_groove",
                    "sectionType": "groove",
                    "startBar": 0,
                    "endBar": 3,
                    "energy": 0.65,
                }
            ],
        },
        "drummer_profile": {
            "primary_style": "rock",
            "timing_tightness": 0.75,
            "ghost_note_frequency": 0.4,
            "preferred_feel": "straight",
        },
    }

    response = client.post("/v1/performance_spec", json=probe)

    assert response.status_code == 503
    payload = response.json()
    assert payload.get("ok") is not True


def test_verified_onnx_performance_spec_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_app_module,
) -> None:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"valid_onnx_payload")
    expected_sha = hashlib.sha256(model_path.read_bytes()).hexdigest()

    monkeypatch.setenv("LLM_STRICT_READINESS", "true")
    monkeypatch.setenv("ONNX_MODEL_PATH", str(model_path))
    monkeypatch.setenv("GENERATION_MODEL_SHA256", expected_sha)
    monkeypatch.setenv("DRUMTRACKAI_MODEL_VERSION", "drum_humanizer_v1.1.17_onnx")

    class _FakeOnnxSession:
        def run(self, _outputs, inputs):
            x = inputs["x"]
            batch_size = int(x.shape[0]) if hasattr(x, "shape") else 1
            return [np.full((batch_size, 9), 0.5, dtype=np.float32)]

    monkeypatch.setattr(llm_app_module, "ACTIVE_BACKEND", "onnx", raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_AVAILABLE", True, raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_SESSION", _FakeOnnxSession(), raising=False)
    monkeypatch.setattr(llm_app_module, "ONNX_MODEL_PATH", model_path, raising=False)
    monkeypatch.setattr(llm_app_module, "MODEL_LOAD_ERROR", None, raising=False)

    client = TestClient(llm_app_module.app)
    probe = {
        "cfg": {
            "sectionId": "calibration_probe",
            "startMeasure": 0,
            "endMeasure": 3,
            "tempos": [110, 110, 110, 110],
            "timeSignature": [4, 4],
            "style": "rock",
            "drummer": "john_bonham",
            "intensity": 0.65,
            "variation": 0.5,
            "complexity": 0.5,
            "humanizeAmount": 0.7,
            "ghostNoteAmount": 0.5,
            "swingAmount": 0.0,
        },
        "songmap_summary": {
            "styleGroup": "rock",
            "sections": [
                {
                    "label": "calibration_groove",
                    "sectionType": "groove",
                    "startBar": 0,
                    "endBar": 3,
                    "energy": 0.65,
                }
            ],
        },
        "drummer_profile": {
            "primary_style": "rock",
            "timing_tightness": 0.75,
            "ghost_note_frequency": 0.4,
            "preferred_feel": "straight",
        },
    }

    response = client.post("/v1/performance_spec", json=probe)

    assert response.status_code == 200
    payload = response.json()
    assert payload.get("ok") is True
    phrases = ((payload.get("spec") or {}).get("phrases") or []) if isinstance(payload, dict) else []
    assert phrases
    metadata = (payload.get("metadata") or {}) if isinstance(payload, dict) else {}
    assert metadata.get("backend") == "onnx"


def test_bootstrap_rejects_output_shape_mismatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bootstrap = _load_bootstrap_module()
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"onnx")

    class _FakeSession:
        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def get_outputs(self):
            return [SimpleNamespace(name="y")]

        def run(self, *_args, **_kwargs):
            return [np.zeros((1, 8), dtype=np.float32)]

        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setattr(bootstrap.onnx, "load", lambda _p: object())
    monkeypatch.setattr(bootstrap.onnx.checker, "check_model", lambda _m: None)
    monkeypatch.setattr(bootstrap.ort, "InferenceSession", lambda *_a, **_k: _FakeSession())

    with pytest.raises(bootstrap.ModelBootstrapError, match=r"Expected model output shape \(1, 9\)"):
        bootstrap.validate_onnx_model(model_path)


def test_bootstrap_rejects_nan_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bootstrap = _load_bootstrap_module()
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"onnx")

    class _FakeSession:
        def get_inputs(self):
            return [SimpleNamespace(name="x")]

        def get_outputs(self):
            return [SimpleNamespace(name="y")]

        def run(self, *_args, **_kwargs):
            arr = np.zeros((1, 9), dtype=np.float32)
            arr[0, 0] = np.nan
            return [arr]

        def get_providers(self):
            return ["CPUExecutionProvider"]

    monkeypatch.setattr(bootstrap.onnx, "load", lambda _p: object())
    monkeypatch.setattr(bootstrap.onnx.checker, "check_model", lambda _m: None)
    monkeypatch.setattr(bootstrap.ort, "InferenceSession", lambda *_a, **_k: _FakeSession())

    with pytest.raises(bootstrap.ModelBootstrapError, match="NaN or infinite"):
        bootstrap.validate_onnx_model(model_path)

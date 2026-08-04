from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


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

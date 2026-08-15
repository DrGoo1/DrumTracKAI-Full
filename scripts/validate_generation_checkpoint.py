from __future__ import annotations

import argparse
import csv
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:
    import torch
    import torch.nn as nn

    TORCH_AVAILABLE = True
except Exception:
    torch = None
    nn = None
    TORCH_AVAILABLE = False

try:
    import onnx

    ONNX_AVAILABLE = True
except Exception:
    onnx = None
    ONNX_AVAILABLE = False

try:
    import onnxruntime as ort

    ORT_AVAILABLE = True
except Exception:
    ort = None
    ORT_AVAILABLE = False

STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
STATUS_MISSING_RUNTIME = "MISSING_RUNTIME"
STATUS_LOAD_ERROR = "LOAD_ERROR"

MODEL_EXTS = {".pth", ".pt", ".onnx"}
TEST_INPUT = np.array([[0.5, 0.0, 0.7]], dtype=np.float32)


if TORCH_AVAILABLE and nn is not None:

    class DrumHumanizationModel(nn.Module):
        def __init__(self, input_size: int = 3, hidden_size: int = 64, output_size: int = 9):
            super().__init__()
            self.encoder = nn.Sequential(
                nn.Linear(input_size, hidden_size),
                nn.ReLU(),
                nn.Dropout(0.2),
                nn.Linear(hidden_size, hidden_size * 2),
                nn.ReLU(),
                nn.Dropout(0.2),
            )
            self.predictor = nn.Sequential(
                nn.Linear(hidden_size * 2, hidden_size),
                nn.ReLU(),
                nn.Linear(hidden_size, output_size),
                nn.Sigmoid(),
            )

        def forward(self, x):
            return self.predictor(self.encoder(x))


else:
    DrumHumanizationModel = None


@dataclass
class ValidationResult:
    source_path: str
    sha256: str
    size_bytes: int
    file_extension: str
    detected_model_format: str
    compatibility_result: bool
    output_shape: Optional[List[int]]
    finite_output: Optional[bool]
    sigmoid_compatible_output: Optional[bool]
    status: str
    validation_error: Optional[str]
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_path": self.source_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "file_extension": self.file_extension,
            "detected_model_format": self.detected_model_format,
            "compatibility_result": self.compatibility_result,
            "output_shape": self.output_shape,
            "finite_output": self.finite_output,
            "sigmoid_compatible_output": self.sigmoid_compatible_output,
            "status": self.status,
            "validation_error": self.validation_error,
            "details": self.details,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sanitize_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return safe[:180]


def _is_finite_and_sigmoid_compatible(arr: np.ndarray) -> Tuple[bool, bool]:
    finite = bool(np.isfinite(arr).all())
    if not finite:
        return False, False
    sigmoid_ok = bool(((arr >= -1e-6) & (arr <= 1.000001)).all())
    return True, sigmoid_ok


def _is_trusted(path: Path, trusted_roots: Sequence[Path]) -> bool:
    resolved = path.resolve()
    for root in trusted_roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except Exception:
            continue
    return False


def _safe_torch_load(path: Path, trusted: bool):
    if not TORCH_AVAILABLE or torch is None:
        raise RuntimeError("torch runtime is not available")

    kwargs: Dict[str, Any] = {"map_location": "cpu"}
    sig = inspect.signature(torch.load)
    supports_weights_only = "weights_only" in sig.parameters

    if supports_weights_only:
        kwargs["weights_only"] = True
        try:
            return torch.load(path, **kwargs)
        except Exception:
            if not trusted:
                raise

    if not trusted:
        raise RuntimeError("untrusted pickle source rejected")

    kwargs.pop("weights_only", None)
    return torch.load(path, **kwargs)


def _extract_state_dict(candidate: Any) -> Optional[Dict[str, Any]]:
    if isinstance(candidate, dict):
        if isinstance(candidate.get("model_state_dict"), dict):
            return candidate["model_state_dict"]
        if isinstance(candidate.get("state_dict"), dict):
            return candidate["state_dict"]
        if all(isinstance(k, str) for k in candidate.keys()):
            return candidate
    return None


def _validate_state_dict(path: Path, trusted: bool) -> ValidationResult:
    if not TORCH_AVAILABLE or torch is None or DrumHumanizationModel is None:
        return ValidationResult(
            source_path=str(path),
            sha256=_sha256_file(path),
            size_bytes=int(path.stat().st_size),
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_MISSING_RUNTIME,
            validation_error="torch runtime unavailable",
            details={},
        )

    sha = _sha256_file(path)
    size = int(path.stat().st_size)

    try:
        checkpoint = _safe_torch_load(path, trusted=trusted)
    except Exception as exc:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_LOAD_ERROR,
            validation_error=f"state_dict_load_error: {exc}",
            details={},
        )

    state_dict = _extract_state_dict(checkpoint)
    if state_dict is None:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_INVALID,
            validation_error="no_compatible_state_dict_found",
            details={},
        )

    model = DrumHumanizationModel(input_size=3, hidden_size=64, output_size=9).to("cpu")
    model.eval()

    try:
        model.load_state_dict(state_dict, strict=True)
    except Exception as exc:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_INVALID,
            validation_error=f"strict_state_dict_load_failed: {exc}",
            details={},
        )

    with torch.no_grad():
        try:
            x = torch.from_numpy(TEST_INPUT)
            y = model(x).detach().cpu().numpy()
        except Exception as exc:
            return ValidationResult(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                file_extension=path.suffix.lower(),
                detected_model_format="pytorch_state_dict",
                compatibility_result=False,
                output_shape=None,
                finite_output=None,
                sigmoid_compatible_output=None,
                status=STATUS_LOAD_ERROR,
                validation_error=f"state_dict_inference_failed: {exc}",
                details={},
            )

    output_shape = list(y.shape)
    finite, sigmoid_ok = _is_finite_and_sigmoid_compatible(y)

    if output_shape != [1, 9]:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=output_shape,
            finite_output=finite,
            sigmoid_compatible_output=sigmoid_ok,
            status=STATUS_INVALID,
            validation_error=f"invalid_output_shape:{output_shape}",
            details={},
        )

    if not finite:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=output_shape,
            finite_output=False,
            sigmoid_compatible_output=False,
            status=STATUS_INVALID,
            validation_error="non_finite_output",
            details={},
        )

    if not sigmoid_ok:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="pytorch_state_dict",
            compatibility_result=False,
            output_shape=output_shape,
            finite_output=True,
            sigmoid_compatible_output=False,
            status=STATUS_INVALID,
            validation_error="output_not_sigmoid_compatible",
            details={},
        )

    return ValidationResult(
        source_path=str(path),
        sha256=sha,
        size_bytes=size,
        file_extension=path.suffix.lower(),
        detected_model_format="pytorch_state_dict",
        compatibility_result=True,
        output_shape=output_shape,
        finite_output=True,
        sigmoid_compatible_output=True,
        status=STATUS_VALID,
        validation_error=None,
        details={"strict_state_dict_load": True},
    )


def _validate_torchscript(path: Path, trusted: bool) -> ValidationResult:
    if not TORCH_AVAILABLE or torch is None:
        return ValidationResult(
            source_path=str(path),
            sha256=_sha256_file(path),
            size_bytes=int(path.stat().st_size),
            file_extension=path.suffix.lower(),
            detected_model_format="torchscript",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_MISSING_RUNTIME,
            validation_error="torch runtime unavailable",
            details={},
        )

    if not trusted:
        return ValidationResult(
            source_path=str(path),
            sha256=_sha256_file(path),
            size_bytes=int(path.stat().st_size),
            file_extension=path.suffix.lower(),
            detected_model_format="torchscript",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_INVALID,
            validation_error="untrusted pickle source rejected",
            details={},
        )

    sha = _sha256_file(path)
    size = int(path.stat().st_size)

    try:
        module = torch.jit.load(str(path), map_location="cpu")
        module.eval()
    except Exception as exc:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="torchscript",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_LOAD_ERROR,
            validation_error=f"torchscript_load_error: {exc}",
            details={},
        )

    with torch.no_grad():
        try:
            x = torch.from_numpy(TEST_INPUT)
            y = module(x)
            if isinstance(y, torch.Tensor):
                arr = y.detach().cpu().numpy()
            else:
                arr = np.asarray(y, dtype=np.float32)
        except Exception as exc:
            return ValidationResult(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                file_extension=path.suffix.lower(),
                detected_model_format="torchscript",
                compatibility_result=False,
                output_shape=None,
                finite_output=None,
                sigmoid_compatible_output=None,
                status=STATUS_LOAD_ERROR,
                validation_error=f"torchscript_inference_failed: {exc}",
                details={},
            )

    output_shape = list(arr.shape)
    finite, sigmoid_ok = _is_finite_and_sigmoid_compatible(arr)

    if output_shape != [1, 9]:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="torchscript",
            compatibility_result=False,
            output_shape=output_shape,
            finite_output=finite,
            sigmoid_compatible_output=sigmoid_ok,
            status=STATUS_INVALID,
            validation_error=f"invalid_output_shape:{output_shape}",
            details={},
        )

    if not finite:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="torchscript",
            compatibility_result=False,
            output_shape=output_shape,
            finite_output=False,
            sigmoid_compatible_output=False,
            status=STATUS_INVALID,
            validation_error="non_finite_output",
            details={},
        )

    if not sigmoid_ok:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="torchscript",
            compatibility_result=False,
            output_shape=output_shape,
            finite_output=True,
            sigmoid_compatible_output=False,
            status=STATUS_INVALID,
            validation_error="output_not_sigmoid_compatible",
            details={},
        )

    return ValidationResult(
        source_path=str(path),
        sha256=sha,
        size_bytes=size,
        file_extension=path.suffix.lower(),
        detected_model_format="torchscript",
        compatibility_result=True,
        output_shape=output_shape,
        finite_output=True,
        sigmoid_compatible_output=True,
        status=STATUS_VALID,
        validation_error=None,
        details={"strict_state_dict_load": False},
    )


def _validate_onnx(path: Path) -> ValidationResult:
    sha = _sha256_file(path)
    size = int(path.stat().st_size)

    if not ONNX_AVAILABLE:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_MISSING_RUNTIME,
            validation_error="onnx runtime package unavailable",
            details={},
        )

    if not ORT_AVAILABLE or ort is None:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_MISSING_RUNTIME,
            validation_error="onnxruntime package unavailable",
            details={},
        )

    try:
        model = onnx.load(str(path))
        onnx.checker.check_model(model)
    except Exception as exc:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_LOAD_ERROR,
            validation_error=f"onnx_check_failed: {exc}",
            details={},
        )

    try:
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    except Exception as exc:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_LOAD_ERROR,
            validation_error=f"onnxruntime_session_failed: {exc}",
            details={},
        )

    inputs = sess.get_inputs()
    outputs = sess.get_outputs()
    if not inputs or not outputs:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_INVALID,
            validation_error="onnx_missing_io",
            details={"input_count": len(inputs), "output_count": len(outputs)},
        )

    input_name = inputs[0].name
    try:
        ort_outputs = sess.run(None, {input_name: TEST_INPUT.astype(np.float32)})
    except Exception as exc:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_LOAD_ERROR,
            validation_error=f"onnx_inference_failed: {exc}",
            details={"input_name": input_name},
        )

    chosen = None
    chosen_shape = None
    finite = None
    sigmoid_ok = None
    for out in ort_outputs:
        arr = np.asarray(out)
        shape = list(arr.shape)
        f_ok, s_ok = _is_finite_and_sigmoid_compatible(arr)
        if shape == [1, 9]:
            chosen = arr
            chosen_shape = shape
            finite = f_ok
            sigmoid_ok = s_ok
            break

    if chosen is None:
        shapes = [list(np.asarray(o).shape) for o in ort_outputs]
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=shapes[0] if shapes else None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_INVALID,
            validation_error=f"no_output_with_shape_[1,9]:{shapes}",
            details={"input_name": input_name, "output_names": [o.name for o in outputs]},
        )

    if not finite:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=chosen_shape,
            finite_output=False,
            sigmoid_compatible_output=False,
            status=STATUS_INVALID,
            validation_error="non_finite_output",
            details={"input_name": input_name, "output_names": [o.name for o in outputs]},
        )

    if not sigmoid_ok:
        return ValidationResult(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            file_extension=path.suffix.lower(),
            detected_model_format="onnx",
            compatibility_result=False,
            output_shape=chosen_shape,
            finite_output=True,
            sigmoid_compatible_output=False,
            status=STATUS_INVALID,
            validation_error="output_not_sigmoid_compatible",
            details={"input_name": input_name, "output_names": [o.name for o in outputs]},
        )

    return ValidationResult(
        source_path=str(path),
        sha256=sha,
        size_bytes=size,
        file_extension=path.suffix.lower(),
        detected_model_format="onnx",
        compatibility_result=True,
        output_shape=chosen_shape,
        finite_output=True,
        sigmoid_compatible_output=True,
        status=STATUS_VALID,
        validation_error=None,
        details={"input_name": input_name, "output_names": [o.name for o in outputs]},
    )


def _validate_single(path: Path, trusted_roots: Sequence[Path]) -> ValidationResult:
    if not path.exists() or not path.is_file():
        return ValidationResult(
            source_path=str(path),
            sha256="",
            size_bytes=0,
            file_extension=path.suffix.lower(),
            detected_model_format="unknown",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_INVALID,
            validation_error="file_not_found",
            details={},
        )

    ext = path.suffix.lower()
    trusted = _is_trusted(path, trusted_roots)

    if ext not in MODEL_EXTS:
        return ValidationResult(
            source_path=str(path),
            sha256=_sha256_file(path),
            size_bytes=int(path.stat().st_size),
            file_extension=ext,
            detected_model_format="unknown",
            compatibility_result=False,
            output_shape=None,
            finite_output=None,
            sigmoid_compatible_output=None,
            status=STATUS_UNSUPPORTED_FORMAT,
            validation_error=f"unsupported_extension:{ext}",
            details={},
        )

    if ext == ".onnx":
        return _validate_onnx(path)

    state_result = _validate_state_dict(path, trusted=trusted)

    if ext == ".pt":
        torchscript_result = _validate_torchscript(path, trusted=trusted)
        if state_result.status == STATUS_VALID:
            return state_result
        if torchscript_result.status == STATUS_VALID:
            torchscript_result.details["state_dict_validation"] = {
                "status": state_result.status,
                "error": state_result.validation_error,
            }
            return torchscript_result

        merged_error = {
            "state_dict": {"status": state_result.status, "error": state_result.validation_error},
            "torchscript": {"status": torchscript_result.status, "error": torchscript_result.validation_error},
        }

        primary_status = STATUS_INVALID
        for s in (state_result.status, torchscript_result.status):
            if s == STATUS_MISSING_RUNTIME:
                primary_status = STATUS_MISSING_RUNTIME
                break
            if s == STATUS_LOAD_ERROR and primary_status != STATUS_MISSING_RUNTIME:
                primary_status = STATUS_LOAD_ERROR

        return ValidationResult(
            source_path=str(path),
            sha256=state_result.sha256 or torchscript_result.sha256,
            size_bytes=state_result.size_bytes,
            file_extension=ext,
            detected_model_format="unknown",
            compatibility_result=False,
            output_shape=state_result.output_shape or torchscript_result.output_shape,
            finite_output=state_result.finite_output if state_result.finite_output is not None else torchscript_result.finite_output,
            sigmoid_compatible_output=(
                state_result.sigmoid_compatible_output
                if state_result.sigmoid_compatible_output is not None
                else torchscript_result.sigmoid_compatible_output
            ),
            status=primary_status,
            validation_error="pt_validation_failed",
            details=merged_error,
        )

    return state_result


def _read_paths_from_csv(csv_path: Path) -> List[Path]:
    paths: List[Path] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            full_name = ""
            for key, value in row.items():
                norm_key = str(key or "").replace("\ufeff", "").replace('"', "").strip().lower()
                if norm_key == "fullname":
                    full_name = str(value or "").strip()
                    break
            if full_name:
                paths.append(Path(full_name))
    return paths


def _write_result(out_dir: Path, result: ValidationResult) -> Path:
    digest = result.sha256[:12] if result.sha256 else "nohash"
    basename = _sanitize_filename(Path(result.source_path).name)
    target = out_dir / f"{basename}.{digest}.json"
    target.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return target


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen = set()
    out: List[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate DrumTracKAI generation checkpoint candidates")
    parser.add_argument("--csv", type=Path, default=None, help="CSV with FullName column (e.g., LOCAL_MODEL_INVENTORY.csv)")
    parser.add_argument("--file", action="append", default=[], help="Individual file path to validate (repeatable)")
    parser.add_argument(
        "--trusted-root",
        action="append",
        default=[
            "F:/DrumTracKAI_v1.1.17",
            "F:/DrumTracKAI_calibration_v2",
            "F:/DrumTracKAI_v1.1.16_Clean",
        ],
        help="Trusted root path for pickle-based loads (repeatable)",
    )
    parser.add_argument("--out-dir", type=Path, default=Path("handoff/calibration_v2/model_validation"))
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("handoff/calibration_v2/GENERATION_MODEL_VALIDATION_SUMMARY.json"),
    )
    args = parser.parse_args()

    candidates: List[Path] = []
    if args.csv:
        candidates.extend(_read_paths_from_csv(args.csv))
    for f in args.file:
        candidates.append(Path(f))

    candidates = _dedupe_paths(candidates)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)

    trusted_roots = [Path(p) for p in args.trusted_root]

    results: List[Dict[str, Any]] = []
    counts = {
        STATUS_VALID: 0,
        STATUS_INVALID: 0,
        STATUS_UNSUPPORTED_FORMAT: 0,
        STATUS_MISSING_RUNTIME: 0,
        STATUS_LOAD_ERROR: 0,
    }

    for candidate in candidates:
        result = _validate_single(candidate, trusted_roots=trusted_roots)
        _write_result(args.out_dir, result)
        payload = result.to_dict()
        results.append(payload)
        counts[result.status] = counts.get(result.status, 0) + 1

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_candidates": len(results),
        "counts": counts,
        "results": results,
    }
    args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps({"total_candidates": len(results), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

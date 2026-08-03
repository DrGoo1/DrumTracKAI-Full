from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional


def _resolve_active_model_path() -> Path:
    explicit = os.getenv("ACTIVE_MODEL_PATH")
    if explicit:
        return Path(explicit)

    active_json = os.getenv("ACTIVE_MODEL_JSON", "/models/production/active_model.json")
    p = Path(active_json)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))

        candidates = data.get("candidates")
        if isinstance(candidates, list):
            for entry in candidates:
                if not isinstance(entry, str) or not entry.strip():
                    continue
                candidate = Path(entry.strip())
                if not candidate.is_absolute():
                    candidate = (p.parent / candidate).resolve()
                if candidate.exists():
                    return candidate

        model_path = data.get("path")
        if isinstance(model_path, str) and model_path.strip():
            candidate = Path(model_path.strip())
            if not candidate.is_absolute():
                candidate = (p.parent / candidate).resolve()
            return candidate

    return Path("/models/checkpoints/best_model.pth")


def _resolve_onnx_model_path(active_path: Optional[Path]) -> Optional[Path]:
    explicit = os.getenv("ONNX_MODEL_PATH")
    if explicit:
        return Path(explicit)

    if active_path is None:
        return None

    try:
        return active_path.with_suffix(".onnx")
    except Exception:
        return Path("/models/checkpoints/best_model.onnx")


def _check_file(path: Optional[Path], *, min_bytes: int) -> Dict[str, object]:
    if path is None:
        return {"path": None, "exists": False, "size": None, "valid": False, "reason": "path_not_resolved"}

    exists = path.exists()
    size = int(path.stat().st_size) if exists else None
    valid = bool(exists and size is not None and size >= min_bytes)
    reason = None
    if not exists:
        reason = "missing"
    elif size is not None and size < min_bytes:
        reason = f"too_small(<{min_bytes})"

    return {
        "path": str(path),
        "exists": exists,
        "size": size,
        "valid": valid,
        "reason": reason,
    }


def validate(*, min_torch_bytes: int, min_onnx_bytes: int) -> Dict[str, object]:
    active = _resolve_active_model_path()
    onnx = _resolve_onnx_model_path(active)

    active_check = _check_file(active, min_bytes=min_torch_bytes)
    onnx_check = _check_file(onnx, min_bytes=min_onnx_bytes)

    backend = str(os.getenv("INFERENCE_BACKEND", "auto")).strip().lower()
    strict_env = str(os.getenv("APP_ENV", "development")).strip().lower() in {"staging", "production", "prod", "live"}

    must_have_torch = backend in {"torch", "auto"}
    must_have_onnx = backend == "onnx"

    failures: List[str] = []
    if must_have_torch and not bool(active_check["valid"]):
        failures.append("active_torch_checkpoint_invalid")
    if must_have_onnx and not bool(onnx_check["valid"]):
        failures.append("onnx_checkpoint_invalid")

    return {
        "ok": len(failures) == 0,
        "strict_env": strict_env,
        "inference_backend": backend,
        "active": active_check,
        "onnx": onnx_check,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate strict generation model checkpoint presence")
    parser.add_argument("--min-torch-bytes", type=int, default=4096)
    parser.add_argument("--min-onnx-bytes", type=int, default=4096)
    args = parser.parse_args()

    result = validate(min_torch_bytes=max(1, int(args.min_torch_bytes)), min_onnx_bytes=max(1, int(args.min_onnx_bytes)))
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

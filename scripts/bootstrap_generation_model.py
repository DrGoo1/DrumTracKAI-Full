from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import boto3
import numpy as np
import onnx
import onnxruntime as ort


class ModelBootstrapError(RuntimeError):
    pass


def required_env(name: str) -> str:
    value = str(os.getenv(name, "")).strip()
    if not value:
        raise ModelBootstrapError(f"{name} is required")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "s3":
        raise ModelBootstrapError(
            "GENERATION_MODEL_S3_URI must use the s3:// scheme"
        )

    bucket = parsed.netloc.strip()
    key = parsed.path.lstrip("/").strip()

    if not bucket or not key:
        raise ModelBootstrapError(
            "GENERATION_MODEL_S3_URI must include a bucket and object key"
        )

    return bucket, key


def validate_onnx_model(path: Path) -> dict[str, object]:
    try:
        model = onnx.load(str(path))
        onnx.checker.check_model(model)
    except Exception as exc:
        raise ModelBootstrapError(
            f"ONNX structural validation failed: {exc}"
        ) from exc

    try:
        session = ort.InferenceSession(
            str(path),
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        raise ModelBootstrapError(
            f"ONNX Runtime could not load the model: {exc}"
        ) from exc

    inputs = session.get_inputs()
    outputs = session.get_outputs()

    if len(inputs) != 1:
        raise ModelBootstrapError(
            f"Expected one model input, found {len(inputs)}"
        )

    probe = np.asarray([[0.5, 0.0, 0.7]], dtype=np.float32)

    try:
        values = session.run(
            None,
            {inputs[0].name: probe},
        )
    except Exception as exc:
        raise ModelBootstrapError(
            f"ONNX probe inference failed: {exc}"
        ) from exc

    if not values:
        raise ModelBootstrapError("ONNX model returned no outputs")

    result = np.asarray(values[0])

    if tuple(result.shape) != (1, 9):
        raise ModelBootstrapError(
            f"Expected model output shape (1, 9), got {tuple(result.shape)}"
        )

    if not np.isfinite(result).all():
        raise ModelBootstrapError(
            "ONNX model returned NaN or infinite values"
        )

    if (result < 0.0).any() or (result > 1.0).any():
        raise ModelBootstrapError(
            "ONNX model returned values outside the expected 0..1 range"
        )

    return {
        "input_name": inputs[0].name,
        "input_shape": list(probe.shape),
        "output_names": [item.name for item in outputs],
        "output_shape": list(result.shape),
        "providers": session.get_providers(),
        "finite": True,
        "range_valid": True,
    }


def main() -> int:
    source_uri = required_env("GENERATION_MODEL_S3_URI")
    expected_sha256 = required_env(
        "GENERATION_MODEL_SHA256"
    ).lower()
    target_path = Path(
        required_env("ONNX_MODEL_PATH")
    ).expanduser()

    if len(expected_sha256) != 64:
        raise ModelBootstrapError(
            "GENERATION_MODEL_SHA256 must be a 64-character SHA-256 digest"
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)

    if target_path.is_file():
        existing_sha256 = sha256_file(target_path)
        if hmac.compare_digest(existing_sha256, expected_sha256):
            validation = validate_onnx_model(target_path)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "source": "existing_verified_file",
                        "target_path": str(target_path),
                        "sha256": existing_sha256,
                        "validation": validation,
                    },
                    sort_keys=True,
                )
            )
            return 0

    bucket, key = parse_s3_uri(source_uri)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".generation_model_",
        suffix=".onnx.download",
        dir=str(target_path.parent),
    )
    os.close(file_descriptor)

    temporary_path = Path(temporary_name)

    try:
        client = boto3.client(
            "s3",
            region_name=str(os.getenv("AWS_REGION", "")).strip() or None,
        )

        client.download_file(
            bucket,
            key,
            str(temporary_path),
        )

        actual_sha256 = sha256_file(temporary_path)

        if not hmac.compare_digest(
            actual_sha256,
            expected_sha256,
        ):
            raise ModelBootstrapError(
                "Downloaded model SHA-256 does not match "
                "GENERATION_MODEL_SHA256"
            )

        validation = validate_onnx_model(temporary_path)

        temporary_path.replace(target_path)

        sidecar_path = target_path.with_suffix(
            target_path.suffix + ".sha256"
        )
        sidecar_path.write_text(
            actual_sha256 + "\n",
            encoding="utf-8",
        )

        print(
            json.dumps(
                {
                    "ok": True,
                    "source": "s3_download",
                    "target_path": str(target_path),
                    "sha256": actual_sha256,
                    "size_bytes": target_path.stat().st_size,
                    "validation": validation,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelBootstrapError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        raise SystemExit(2)

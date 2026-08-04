from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlparse

import boto3


class ModelUploadError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "s3":
        raise ModelUploadError("--s3-uri must use s3:// scheme")
    bucket = parsed.netloc.strip()
    key = parsed.path.lstrip("/").strip()
    if not bucket or not key:
        raise ModelUploadError("--s3-uri must include bucket and object key")
    return bucket, key


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload validated generation model to immutable S3 key")
    parser.add_argument("--file", required=True)
    parser.add_argument("--s3-uri", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--model-version", required=True)
    args = parser.parse_args()

    source = Path(args.file)
    if not source.is_file():
        raise ModelUploadError(f"Model file not found: {source}")

    expected = str(args.expected_sha256).strip().lower()
    if len(expected) != 64:
        raise ModelUploadError("--expected-sha256 must be a 64-character SHA-256 digest")

    actual = sha256_file(source)
    if not hmac.compare_digest(actual, expected):
        raise ModelUploadError("Local file SHA-256 mismatch; upload aborted")

    bucket, key = parse_s3_uri(args.s3_uri)

    client = boto3.client("s3")
    client.upload_file(
        str(source),
        bucket,
        key,
        ExtraArgs={
            "Metadata": {
                "sha256": actual,
                "model-version": str(args.model_version),
                "model-family": "drum_humanization",
                "model-format": "onnx",
            }
        },
    )

    head = client.head_object(Bucket=bucket, Key=key)
    remote_size = int(head.get("ContentLength", 0))
    local_size = int(source.stat().st_size)
    if remote_size != local_size:
        raise ModelUploadError(
            f"Uploaded object size mismatch (remote={remote_size}, local={local_size})"
        )

    remote_meta = {str(k).lower(): str(v) for k, v in (head.get("Metadata") or {}).items()}
    if remote_meta.get("sha256", "").lower() != actual:
        raise ModelUploadError("Uploaded object metadata sha256 mismatch")

    print(
        json.dumps(
            {
                "ok": True,
                "bucket": bucket,
                "key": key,
                "sha256": actual,
                "size_bytes": local_size,
                "model_version": str(args.model_version),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ModelUploadError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        raise SystemExit(2)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"upload_failed: {type(exc).__name__}"}, sort_keys=True))
        raise SystemExit(2)

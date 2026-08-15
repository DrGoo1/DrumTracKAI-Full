from __future__ import annotations

"""Validate and publish an owned/licensed calibration multisample kit to S3."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Dict, Iterable, Mapping, Tuple

import boto3


class SamplePackPublishError(RuntimeError):
    pass


def _load_renderer_helpers():
    module_path = Path(__file__).resolve().with_name("calibration_sample_renderer.py")
    spec = importlib.util.spec_from_file_location("calibration_sample_renderer", module_path)
    if spec is None or spec.loader is None:
        raise SamplePackPublishError("Could not load calibration_sample_renderer helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_key_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(value or "").replace("\\", "/"))
    normalized = re.sub(r"/+", "/", normalized).strip("/.")
    if not normalized or ".." in PurePosixPath(normalized).parts:
        raise SamplePackPublishError(f"Unsafe sample path: {value!r}")
    return normalized


def _round_robin_entries(manifest: Mapping[str, Any]) -> Iterable[Tuple[str, str, int, int, str]]:
    articulations = manifest.get("articulations")
    if not isinstance(articulations, dict) or not articulations:
        raise SamplePackPublishError("Manifest has no articulations")
    for articulation_id, articulation in articulations.items():
        if not isinstance(articulation, dict):
            continue
        mics = articulation.get("mics")
        if not isinstance(mics, dict) or not mics:
            raise SamplePackPublishError(f"Articulation {articulation_id!r} has no mics")
        for mic_id, mic in mics.items():
            if not isinstance(mic, dict):
                continue
            layers = mic.get("velocityLayers")
            if not isinstance(layers, list) or not layers:
                raise SamplePackPublishError(
                    f"Articulation {articulation_id!r} mic {mic_id!r} has no velocityLayers"
                )
            for layer_index, layer in enumerate(layers):
                if not isinstance(layer, dict):
                    continue
                round_robin = layer.get("roundRobin")
                if not isinstance(round_robin, list) or not round_robin:
                    raise SamplePackPublishError(
                        f"Articulation {articulation_id!r} mic {mic_id!r} layer {layer_index} has no roundRobin samples"
                    )
                for rr_index, raw_path in enumerate(round_robin):
                    path = str(raw_path or "").strip()
                    if not path:
                        raise SamplePackPublishError("Manifest contains an empty sample path")
                    yield str(articulation_id), str(mic_id), layer_index, rr_index, path


def _s3_client(region: str):
    kwargs: Dict[str, Any] = {"region_name": region or None}
    endpoint = str(os.getenv("AWS_S3_ENDPOINT_URL") or "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _put_file(
    *,
    client,
    bucket: str,
    key: str,
    content: bytes,
    content_type: str,
    metadata: Dict[str, str],
    encryption: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    kwargs: Dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": content,
        "ContentType": content_type,
        "Metadata": metadata,
    }
    if encryption:
        kwargs["ServerSideEncryption"] = encryption
    client.put_object(**kwargs)
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength") or 0) != len(content):
        raise SamplePackPublishError(f"Uploaded size mismatch for s3://{bucket}/{key}")


def publish(
    *,
    manifest_path: Path,
    samples_root: Path,
    bucket: str,
    prefix: str,
    expected_version: str,
    region: str,
    dry_run: bool,
) -> Dict[str, Any]:
    renderer = _load_renderer_helpers()
    if not manifest_path.is_file():
        raise SamplePackPublishError(f"Manifest not found: {manifest_path}")
    if not samples_root.is_dir():
        raise SamplePackPublishError(f"Samples root not found: {samples_root}")

    raw_manifest = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw_manifest.decode("utf-8-sig"))
    except Exception as exc:
        raise SamplePackPublishError(f"Manifest JSON is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SamplePackPublishError("Manifest root must be an object")

    version = str(manifest.get("version") or "").strip()
    if not version:
        raise SamplePackPublishError("Manifest version is required")
    if expected_version and version != expected_version:
        raise SamplePackPublishError(
            f"Manifest version {version!r} does not match expected version {expected_version!r}"
        )

    prefix_value = _safe_key_component(prefix)
    client = _s3_client(region)
    encryption = str(os.getenv("AWS_S3_SERVER_SIDE_ENCRYPTION", "AES256")).strip()
    uploaded: Dict[str, Dict[str, Any]] = {}

    for articulation_id, mic_id, layer_index, rr_index, raw_path in _round_robin_entries(manifest):
        parsed_scheme = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(raw_path).scheme.lower()
        if parsed_scheme:
            raise SamplePackPublishError(
                "Publisher expects local relative sample paths; found URI "
                f"{raw_path!r} at {articulation_id}/{mic_id}/{layer_index}/{rr_index}"
            )
        relative = _safe_key_component(raw_path)
        local_path = (samples_root / Path(relative)).resolve()
        try:
            local_path.relative_to(samples_root.resolve())
        except ValueError as exc:
            raise SamplePackPublishError(f"Sample escapes samples root: {raw_path}") from exc
        if not local_path.is_file():
            raise SamplePackPublishError(f"Sample not found: {local_path}")

        content = local_path.read_bytes()
        renderer._decode_wav(content)
        digest = _sha256_bytes(content)
        key = f"{prefix_value}/{relative}"
        if relative not in uploaded:
            _put_file(
                client=client,
                bucket=bucket,
                key=key,
                content=content,
                content_type="audio/wav",
                metadata={
                    "sha256": digest,
                    "sample-pack-version": version,
                    "articulation": articulation_id,
                    "mic": mic_id,
                },
                encryption=encryption,
                dry_run=dry_run,
            )
            uploaded[relative] = {
                "key": key,
                "sha256": digest,
                "size_bytes": len(content),
            }

    canonical_manifest = json.dumps(
        manifest,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    manifest_sha256 = _sha256_bytes(canonical_manifest)
    manifest_key = f"{prefix_value}/kit_manifest_v1.json"
    _put_file(
        client=client,
        bucket=bucket,
        key=manifest_key,
        content=canonical_manifest,
        content_type="application/json",
        metadata={
            "sha256": manifest_sha256,
            "sample-pack-version": version,
            "sample-count": str(len(uploaded)),
        },
        encryption=encryption,
        dry_run=dry_run,
    )

    return {
        "ok": True,
        "dry_run": dry_run,
        "sample_pack_version": version,
        "sample_count": len(uploaded),
        "manifest_sha256": manifest_sha256,
        "manifest_s3_uri": f"s3://{bucket}/{manifest_key}",
        "prefix": prefix_value,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish Calibration v2 sample pack")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--samples-root", required=True)
    parser.add_argument("--bucket", default=os.getenv("AWS_S3_BUCKET", ""))
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--expected-version", default="")
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        bucket = str(args.bucket or "").strip()
        if not bucket:
            raise SamplePackPublishError("--bucket or AWS_S3_BUCKET is required")
        result = publish(
            manifest_path=Path(args.manifest).expanduser().resolve(),
            samples_root=Path(args.samples_root).expanduser().resolve(),
            bucket=bucket,
            prefix=str(args.prefix),
            expected_version=str(args.expected_version or "").strip(),
            region=str(args.region or "").strip(),
            dry_run=bool(args.dry_run),
        )
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

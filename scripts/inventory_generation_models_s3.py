from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List

import boto3

MODEL_EXTENSIONS = (".pth", ".pt", ".onnx")
DEFAULT_PREFIXES = (
    "models/",
    "model/",
    "checkpoints/",
    "production/",
    "admin/models/",
    "llm/",
)


def _matches_model_key(key: str) -> bool:
    key_l = key.lower()
    return key_l.endswith(MODEL_EXTENSIONS)


def _to_record(bucket: str, obj: Dict[str, object]) -> Dict[str, object]:
    last_modified = obj.get("LastModified")
    iso = None
    if isinstance(last_modified, datetime):
        iso = last_modified.astimezone(timezone.utc).isoformat()
    return {
        "bucket": bucket,
        "key": str(obj.get("Key", "")),
        "size": int(obj.get("Size", 0)),
        "last_modified": iso,
        "etag": str(obj.get("ETag", "")).strip('"'),
    }


def _list_with_prefixes(s3_client, bucket: str, prefixes: Iterable[str]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    seen = set()
    for prefix in prefixes:
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = str(obj.get("Key", ""))
                if not key or key in seen or not _matches_model_key(key):
                    continue
                seen.add(key)
                out.append(_to_record(bucket, obj))
    return out


def _list_bucket_wide_filtered(s3_client, bucket: str) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []) or []:
            key = str(obj.get("Key", ""))
            if not key or not _matches_model_key(key):
                continue
            out.append(_to_record(bucket, obj))
    return out


def main() -> int:
    bucket = str(os.getenv("AWS_S3_BUCKET", "")).strip()
    out_path = Path("handoff/calibration_v2/S3_MODEL_INVENTORY.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not bucket:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bucket": None,
            "method": "not_run",
            "prefixes": list(DEFAULT_PREFIXES),
            "count": 0,
            "objects": [],
            "error": "AWS_S3_BUCKET is required",
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote 0 model objects to {out_path} (missing AWS_S3_BUCKET)")
        return 2

    s3 = boto3.client("s3")
    records = _list_with_prefixes(s3, bucket=bucket, prefixes=DEFAULT_PREFIXES)
    method = "prefix_scan"

    if not records:
        records = _list_bucket_wide_filtered(s3, bucket=bucket)
        method = "bucket_wide_scan"

    records.sort(key=lambda r: (int(r.get("size", 0)), str(r.get("key", ""))), reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bucket": bucket,
        "method": method,
        "prefixes": list(DEFAULT_PREFIXES),
        "count": len(records),
        "objects": records,
    }

    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(records)} model objects to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

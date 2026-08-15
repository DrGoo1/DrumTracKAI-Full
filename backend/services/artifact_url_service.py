from __future__ import annotations

import logging
import os
from typing import Optional, Set
from urllib.parse import urlparse

import boto3


logger = logging.getLogger(__name__)


class ArtifactUrlService:
    """Resolve durable artifact URIs into browser-playable URLs.

    Production and staging artifacts are expected to live in S3 (or already be
    represented by HTTPS URLs).  Local artifact paths are supported only in a
    development environment because Render services do not share filesystems.
    """

    def __init__(self) -> None:
        self._region = str(
            os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or ""
        ).strip() or None
        self._endpoint_url = str(os.getenv("AWS_S3_ENDPOINT_URL") or "").strip() or None
        try:
            ttl = int(str(os.getenv("AWS_S3_SIGNED_URL_TTL_SEC", "900")).strip() or "900")
        except Exception:
            ttl = 900
        self._ttl_seconds = max(60, min(ttl, 3600))
        self._s3 = None

    @staticmethod
    def _app_env() -> str:
        return str(os.getenv("APP_ENV", "development")).strip().lower()

    @classmethod
    def _local_paths_allowed(cls) -> bool:
        return cls._app_env() in {"development", "dev", "local", "test"}

    @staticmethod
    def _allowed_buckets() -> Set[str]:
        values = []
        primary = str(os.getenv("AWS_S3_BUCKET") or "").strip()
        if primary:
            values.append(primary)
        extra = str(os.getenv("CALIBRATION_ARTIFACT_BUCKETS") or "").strip()
        if extra:
            values.extend(item.strip() for item in extra.split(",") if item.strip())
        return {item for item in values if item}

    def _client(self):
        if self._s3 is None:
            kwargs = {"region_name": self._region}
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            self._s3 = boto3.client("s3", **kwargs)
        return self._s3

    def _presign_s3(self, *, bucket: str, key: str) -> Optional[str]:
        allowed = self._allowed_buckets()
        if allowed and bucket not in allowed:
            logger.warning("artifact_url_bucket_rejected bucket=%s", bucket)
            return None
        try:
            return self._client().generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=self._ttl_seconds,
            )
        except Exception as exc:
            logger.warning(
                "artifact_url_presign_failed bucket=%s key=%s error=%s",
                bucket,
                key,
                exc,
            )
            return None

    def build_url(self, storage_uri: str) -> Optional[str]:
        if not storage_uri:
            return None
        uri = str(storage_uri).strip()
        if not uri:
            return None

        normalized = uri.replace("\\", "/")
        parsed = urlparse(normalized)
        scheme = parsed.scheme.lower()

        if scheme == "s3":
            bucket = parsed.netloc.strip()
            key = parsed.path.lstrip("/").strip()
            if not bucket or not key:
                return None
            return self._presign_s3(bucket=bucket, key=key)

        if scheme in {"http", "https"}:
            return normalized

        if not self._local_paths_allowed():
            return None

        if normalized.startswith("/static/calibration_artifacts/"):
            return normalized

        marker = "artifacts/calibration/"
        marker_index = normalized.lower().find(marker)
        if marker_index != -1:
            relative = normalized[marker_index + len(marker):].lstrip("/")
            return f"/static/calibration_artifacts/{relative}"

        if scheme == "file":
            local_path = parsed.path or ""
            marker_index = local_path.lower().find(marker)
            if marker_index != -1:
                relative = local_path[marker_index + len(marker):].lstrip("/")
                return f"/static/calibration_artifacts/{relative}"
            return None

        if scheme:
            return None

        if normalized.startswith("/"):
            return normalized
        return "/" + normalized.lstrip("/")

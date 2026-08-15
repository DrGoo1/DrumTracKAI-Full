from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict
import logging
import hashlib
import os
from datetime import datetime

from admin.services.central_database_service import CentralDatabaseService


logger = logging.getLogger(__name__)


@dataclass
class RenderRequest:
    run_id: str
    render_profile_id: str
    sample_pack_version: str
    kit_id: str
    seed: int
    render_recipe: Dict[str, Any]


class CalibrationRenderService:
    """Queue-backed calibration render service.

    This service never synthesizes fallback audio. It enqueues durable render jobs
    and annotates run metadata so a worker can perform real artifact generation.
    """

    def __init__(self, db: CentralDatabaseService) -> None:
        self._db = db

    def _database_url_fingerprint(self) -> str:
        database_url = str(os.getenv("DATABASE_URL") or "").strip()
        if not database_url:
            return ""
        return hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:16]

    def render_run(self, request: RenderRequest) -> None:
        try:
            run_id = str(request.run_id or "").strip()
            if not run_id:
                raise ValueError("render_run requires run_id")

            api_db_fingerprint = self._database_url_fingerprint()
            worker_db_fingerprint = str(os.getenv("CALIBRATION_RENDER_WORKER_DB_FINGERPRINT") or "").strip()
            database_url_match = (not api_db_fingerprint) or (not worker_db_fingerprint) or (api_db_fingerprint == worker_db_fingerprint)
            if not database_url_match:
                raise RuntimeError(
                    "Database fingerprint mismatch between API and worker. "
                    f"api={api_db_fingerprint or 'unknown'} worker={worker_db_fingerprint or 'unknown'}"
                )

            render_profile_id = str(request.render_profile_id or "").strip() or "calibration_standard_v1"
            sample_pack_version = str(request.sample_pack_version or "").strip() or "default"
            run_ref = self._db.get_calibration_run(run_id=request.run_id)
            meta: Dict[str, Any] = {}
            if run_ref and isinstance(run_ref.metadata, dict):
                meta = dict(run_ref.metadata)

            render_meta = meta.get("render") if isinstance(meta.get("render"), dict) else {}
            request_payload = {
                "run_id": run_id,
                "render_profile_id": render_profile_id,
                "sample_pack_version": sample_pack_version,
                "kit_id": str(request.kit_id or "default_kit"),
                "seed": int(request.seed),
                "render_recipe": dict(request.render_recipe or {}),
            }

            existing_job_id = str(render_meta.get("job_id") or "").strip()
            if existing_job_id:
                job_id = self._db.log_calibration_render_job(
                    run_id=run_id,
                    render_profile_id=render_profile_id,
                    sample_pack_version=sample_pack_version,
                    status="queued",
                    artifact_ids=[],
                    error_text=None,
                    job_id=existing_job_id,
                )
            else:
                job_id = self._db.log_calibration_render_job(
                    run_id=run_id,
                    render_profile_id=render_profile_id,
                    sample_pack_version=sample_pack_version,
                    status="queued",
                    artifact_ids=[],
                    error_text=None,
                    job_id=f"rjob_{run_id}",
                )
            if not job_id:
                raise RuntimeError(f"Unable to enqueue calibration render job for run_id={run_id}")

            meta.setdefault("render", {})
            meta["render"].update(
                {
                    "status": "queued",
                    "queued_at": datetime.utcnow().isoformat(),
                    "render_profile_id": render_profile_id,
                    "sample_pack_version": sample_pack_version,
                    "kit_id": request.kit_id,
                    "seed": int(request.seed),
                    "job_id": job_id,
                    "request": request_payload,
                    "api_db_fingerprint": api_db_fingerprint,
                    "worker_db_fingerprint": worker_db_fingerprint or None,
                    "database_url_match": bool(database_url_match),
                    "stub_mode": "queue_only_real_worker_required",
                }
            )
            self._db.log_calibration_run(
                drummer_slug=run_ref.drummer_slug if run_ref else "unknown",
                outcome="queued",
                metadata=meta,
                run_id=request.run_id,
            )
        except Exception:
            logger.exception("render_run_failed run_id=%s", request.run_id)
            raise

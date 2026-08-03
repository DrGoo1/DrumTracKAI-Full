from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import argparse
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from admin.services.central_database_service import CentralDatabaseService, CalibrationRenderJob


logger = logging.getLogger(__name__)


@dataclass
class WorkerRunSummary:
    processed: int = 0
    completed: int = 0
    retried: int = 0
    failed: int = 0
    skipped: int = 0


class CalibrationRenderWorker:
    """Processes queued calibration render jobs and logs real audio artifacts.

    The worker is intentionally command-driven so production can bind to the
    actual renderer (plugin host, render farm, or another service) without
    coupling this API process to a specific audio engine implementation.
    """

    def __init__(
        self,
        db: CentralDatabaseService,
        *,
        command_template: Optional[str] = None,
        max_retries: Optional[int] = None,
        poll_interval_sec: Optional[float] = None,
        command_timeout_sec: Optional[int] = None,
    ) -> None:
        self._db = db
        self._command_template = (
            str(command_template or os.getenv("CALIBRATION_RENDER_WORKER_COMMAND", "")).strip()
        )
        self._max_retries = int(max_retries if max_retries is not None else os.getenv("CALIBRATION_RENDER_MAX_RETRIES", "3") or "3")
        self._poll_interval_sec = float(
            poll_interval_sec if poll_interval_sec is not None else os.getenv("CALIBRATION_RENDER_WORKER_POLL_SEC", "2.0") or "2.0"
        )
        self._command_timeout_sec = int(
            command_timeout_sec if command_timeout_sec is not None else os.getenv("CALIBRATION_RENDER_COMMAND_TIMEOUT_SEC", "300") or "300"
        )

    @property
    def command_configured(self) -> bool:
        return bool(self._command_template)

    def run_once(self, *, max_jobs: int = 2) -> Dict[str, Any]:
        summary = WorkerRunSummary()
        jobs = self._db.list_calibration_render_jobs(statuses=["queued", "retry"], limit=max(1, int(max_jobs)))
        for job in jobs:
            if not self._db.claim_calibration_render_job(job_id=job.job_id, from_statuses=["queued", "retry"]):
                summary.skipped += 1
                continue
            summary.processed += 1
            outcome = self._process_claimed_job(job)
            if outcome == "completed":
                summary.completed += 1
            elif outcome == "retry":
                summary.retried += 1
            elif outcome == "failed":
                summary.failed += 1
            else:
                summary.skipped += 1

        return {
            "processed": summary.processed,
            "completed": summary.completed,
            "retried": summary.retried,
            "failed": summary.failed,
            "skipped": summary.skipped,
            "queued_remaining": len(self._db.list_calibration_render_jobs(statuses=["queued", "retry"], limit=200)),
            "command_configured": self.command_configured,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def run_forever(self, *, stop_flag: Optional[Dict[str, Any]] = None) -> None:
        while True:
            if stop_flag and bool(stop_flag.get("stop")):
                return
            try:
                self.run_once(max_jobs=4)
            except Exception:
                logger.exception("calibration_render_worker_cycle_failed")
            time.sleep(max(0.25, self._poll_interval_sec))

    def _process_claimed_job(self, job: CalibrationRenderJob) -> str:
        run_id = str(job.run_id or "").strip()
        if not run_id:
            self._db.update_calibration_render_job_status(
                job_id=job.job_id,
                status="failed",
                artifact_ids=[],
                error_text="Job missing run_id",
            )
            return "failed"

        run = self._db.get_calibration_run(run_id=run_id)
        if not run:
            self._db.update_calibration_render_job_status(
                job_id=job.job_id,
                status="failed",
                artifact_ids=[],
                error_text=f"Calibration run not found for run_id={run_id}",
            )
            return "failed"

        meta = dict(run.metadata or {})
        render_meta = meta.get("render") if isinstance(meta.get("render"), dict) else {}
        attempts = int(render_meta.get("attempt_count") or 0) + 1

        self._update_run_render_meta(
            run_id=run_id,
            run=run,
            render_patch={
                "status": "running",
                "attempt_count": attempts,
                "last_attempt_at": datetime.utcnow().isoformat(),
                "job_id": job.job_id,
            },
            outcome="running",
        )

        try:
            artifacts = self._invoke_renderer(job=job, run=run, attempts=attempts)
            if not artifacts:
                raise RuntimeError("Renderer returned no artifacts")

            logged_artifact_ids: List[str] = []
            for artifact in artifacts:
                artifact_id = self._db.log_audio_artifact(
                    run_id=run_id,
                    artifact_type=str(artifact.get("artifact_type") or "candidate_audio"),
                    storage_uri=str(artifact.get("storage_uri") or "").strip(),
                    duration_sec=self._safe_float(artifact.get("duration_sec")),
                    loudness_lufs=self._safe_float(artifact.get("loudness_lufs")),
                    sample_pack_version=str(artifact.get("sample_pack_version") or job.sample_pack_version or "default"),
                    render_recipe=(artifact.get("render_recipe") if isinstance(artifact.get("render_recipe"), dict) else {}) or {},
                    artifact_id=(str(artifact.get("artifact_id") or "").strip() or None),
                )
                if artifact_id:
                    logged_artifact_ids.append(artifact_id)

            if not logged_artifact_ids:
                raise RuntimeError("Renderer output could not be persisted as audio artifacts")

            self._db.update_calibration_render_job_status(
                job_id=job.job_id,
                status="completed",
                artifact_ids=logged_artifact_ids,
                error_text=None,
            )
            self._update_run_render_meta(
                run_id=run_id,
                run=run,
                render_patch={
                    "status": "completed",
                    "completed_at": datetime.utcnow().isoformat(),
                    "artifact_ids": logged_artifact_ids,
                    "last_error": None,
                },
                outcome="success",
            )
            return "completed"
        except Exception as exc:
            error_text = str(exc)
            should_retry = attempts < max(1, self._max_retries)
            next_status = "retry" if should_retry else "failed"
            self._db.update_calibration_render_job_status(
                job_id=job.job_id,
                status=next_status,
                artifact_ids=[],
                error_text=error_text,
            )
            self._update_run_render_meta(
                run_id=run_id,
                run=run,
                render_patch={
                    "status": next_status,
                    "last_error": error_text,
                    "attempt_count": attempts,
                    "failed_at": datetime.utcnow().isoformat(),
                },
                outcome=("queued" if should_retry else "failure"),
            )
            logger.exception("calibration_render_job_failed run_id=%s job_id=%s", run_id, job.job_id)
            return next_status

    def _invoke_renderer(self, *, job: CalibrationRenderJob, run: Any, attempts: int) -> List[Dict[str, Any]]:
        if not self._command_template:
            raise RuntimeError("CALIBRATION_RENDER_WORKER_COMMAND is not configured")

        run_event_payload = self._db.get_calibration_run_events_payload(run_id=run.run_id) or {}
        meta = dict(run.metadata or {})
        render_meta = meta.get("render") if isinstance(meta.get("render"), dict) else {}

        request_payload = render_meta.get("request") if isinstance(render_meta.get("request"), dict) else {}
        payload = {
            "job": {
                "job_id": job.job_id,
                "run_id": job.run_id,
                "render_profile_id": job.render_profile_id,
                "sample_pack_version": job.sample_pack_version,
                "attempt": attempts,
            },
            "run": {
                "run_id": run.run_id,
                "drummer_slug": run.drummer_slug,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "metadata": meta,
            },
            "render_request": request_payload,
            "run_events": run_event_payload,
        }

        with tempfile.TemporaryDirectory(prefix="calib_render_") as temp_dir:
            temp_path = Path(temp_dir)
            input_path = temp_path / "render_request.json"
            output_path = temp_path / "render_result.json"
            input_path.write_text(json.dumps(payload, ensure_ascii=True, default=str), encoding="utf-8")

            command = self._build_command(input_path=input_path, output_path=output_path)
            proc = subprocess.run(
                command,
                cwd=str(Path(__file__).resolve().parents[2]),
                capture_output=True,
                text=True,
                timeout=max(15, self._command_timeout_sec),
                shell=False,
                check=False,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or "").strip()
                stdout = (proc.stdout or "").strip()
                raise RuntimeError(
                    f"Render command failed with exit code {proc.returncode}. stdout={stdout[:4000]} stderr={stderr[:4000]}"
                )

            result_payload: Dict[str, Any] = {}
            if output_path.is_file():
                try:
                    result_payload = json.loads(output_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    raise RuntimeError(f"Render result JSON parse failed: {exc}") from exc
            elif (proc.stdout or "").strip().startswith("{"):
                try:
                    result_payload = json.loads(proc.stdout or "{}")
                except Exception as exc:
                    raise RuntimeError(f"Render stdout JSON parse failed: {exc}") from exc
            else:
                raise RuntimeError("Render command produced no result payload")

        artifacts = result_payload.get("artifacts") if isinstance(result_payload.get("artifacts"), list) else []
        normalized: List[Dict[str, Any]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            storage_uri = str(artifact.get("storage_uri") or "").strip()
            artifact_type = str(artifact.get("artifact_type") or "candidate_audio").strip()
            duration_sec = self._safe_float(artifact.get("duration_sec"))
            sample_pack_version = str(artifact.get("sample_pack_version") or job.sample_pack_version or "").strip()
            render_recipe = (artifact.get("render_recipe") if isinstance(artifact.get("render_recipe"), dict) else {}) or {}
            if not storage_uri or not artifact_type or not self._storage_uri_allowed(storage_uri):
                continue
            if self._is_production() and (duration_sec is None or duration_sec <= 0 or not sample_pack_version):
                continue
            render_recipe = {
                **render_recipe,
                "renderer_version": str(
                    render_recipe.get("renderer_version")
                    or artifact.get("renderer_version")
                    or os.getenv("CALIBRATION_RENDERER_VERSION", "unknown")
                ),
                "sample_pack_version": sample_pack_version,
                "sha256": str(render_recipe.get("sha256") or artifact.get("sha256") or "").strip() or None,
                "true_peak_db": self._safe_float(
                    render_recipe.get("true_peak_db")
                    if render_recipe.get("true_peak_db") is not None
                    else artifact.get("true_peak_db")
                ),
            }
            if self._is_production() and render_recipe["renderer_version"] in {"", "unknown"}:
                continue
            normalized.append(
                {
                    "artifact_id": str(artifact.get("artifact_id") or "").strip() or None,
                    "artifact_type": artifact_type,
                    "storage_uri": storage_uri,
                    "duration_sec": duration_sec,
                    "loudness_lufs": self._safe_float(artifact.get("loudness_lufs")),
                    "sample_pack_version": sample_pack_version or None,
                    "render_recipe": render_recipe,
                }
            )

        if not normalized:
            raise RuntimeError("Render command did not return any valid artifacts")
        return normalized

    def _build_command(self, *, input_path: Path, output_path: Path) -> List[str]:
        template = self._command_template.strip()
        if not template:
            raise RuntimeError("Missing render command template")
        formatted = template.replace("{input}", str(input_path)).replace("{output}", str(output_path))
        return shlex.split(formatted, posix=(os.name != "nt"))

    def _update_run_render_meta(
        self,
        *,
        run_id: str,
        run: Any,
        render_patch: Dict[str, Any],
        outcome: str,
    ) -> None:
        existing_meta = dict(run.metadata or {})
        existing_render = existing_meta.get("render") if isinstance(existing_meta.get("render"), dict) else {}
        merged = dict(existing_render)
        merged.update(render_patch or {})
        existing_meta["render"] = merged

        completed_at = datetime.utcnow() if outcome in {"success", "failure"} else None
        self._db.log_calibration_run(
            drummer_slug=run.drummer_slug,
            outcome=outcome,
            metadata=existing_meta,
            completed_at=completed_at,
            run_id=run_id,
        )

    @staticmethod
    def _is_production() -> bool:
        return str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development"))).strip().lower() in {
            "production",
            "prod",
            "live",
        }

    @classmethod
    def _storage_uri_allowed(cls, storage_uri: str) -> bool:
        raw = str(storage_uri or "").strip()
        parsed = urlparse(raw)
        if parsed.scheme.lower() in {"s3", "https"}:
            return bool(parsed.netloc and parsed.path.lstrip("/"))
        if parsed.scheme.lower() == "file":
            return (not cls._is_production()) and bool(parsed.path)
        if cls._is_production():
            return False
        if len(raw) >= 3 and raw[1] == ":" and raw[2] in {"\\", "/"}:
            return True
        return not parsed.scheme and bool(raw)

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None


def _main() -> int:
    parser = argparse.ArgumentParser(description="Calibration render worker")
    parser.add_argument("--once", action="store_true", help="Process up to --max-jobs once and exit")
    parser.add_argument("--max-jobs", type=int, default=4, help="Maximum jobs per cycle")
    args = parser.parse_args()

    db = CentralDatabaseService.get_instance()
    db.initialize()
    worker = CalibrationRenderWorker(db)

    if args.once:
        summary = worker.run_once(max_jobs=max(1, int(args.max_jobs)))
        print(json.dumps(summary, ensure_ascii=True))
        return 0

    worker.run_forever(stop_flag=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

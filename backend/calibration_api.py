"""REST API for calibration lab."""
from __future__ import annotations

import json
import logging
import threading
import asyncio
import time
import hashlib
from datetime import datetime
import os
import base64
import uuid
from pathlib import Path
from urllib.parse import urlparse, quote
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Literal

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, status, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from admin.services.central_database_service import CentralDatabaseService
from sqlalchemy import create_engine, text  # type: ignore

import requests  # type: ignore
from jose import jwt  # type: ignore

from backend.services.artifact_url_service import ArtifactUrlService
from backend.services.calibration_render_service import CalibrationRenderService, RenderRequest
from backend.services.calibration_candidate_generator import generate_candidate_run
from backend.calibration_v2_api import router as calibration_v2_router
from backend.app.assimilation.api.routes_drummer_generation import (
    router as assimilation_generation_router,
)

if TYPE_CHECKING:
    from admin.services.central_database_service import (
        AudioArtifact,
        CalibrationFeedback,
        CalibrationRun,
        EvaluationItem,
        EvaluationSession,
        RunVersion,
    )

router = APIRouter(prefix="/calibration", tags=["calibration"])

_artifact_url_service = ArtifactUrlService()
logger = logging.getLogger(__name__)

CALIBRATION_API_BUILD_MARKER = os.getenv(
    "CALIBRATION_API_BUILD_MARKER",
    "calibration_api_build_2026-06-19_strict_baseline_downgrade_v2",
)
CALIBRATION_API_INSTANCE_ID = (
    os.getenv("RENDER_INSTANCE_ID")
    or os.getenv("HOSTNAME")
    or uuid.uuid4().hex[:12]
)


def _runtime_diagnostics() -> Dict[str, Any]:
    diagnostics: Dict[str, Any] = {
        "api_build_marker": CALIBRATION_API_BUILD_MARKER,
        "api_instance_id": CALIBRATION_API_INSTANCE_ID,
        "pid": os.getpid(),
        "db_backend": str(os.getenv("DB_BACKEND", "")).strip().lower() or None,
        "database_url_configured": bool(str(os.getenv("DATABASE_URL", "")).strip()),
    }
    for key in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION", "RENDER_SERVICE_NAME"):
        value = str(os.getenv(key, "")).strip()
        if value:
            diagnostics[key] = value
    return diagnostics

_DB_INIT_LOCK = threading.RLock()
_DB_SERVICE_READY = False

_AUTO_ASSIMILATION_LOCK = threading.Lock()
_AUTO_ASSIMILATION_STATE: Dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_completed_at": None,
    "last_error": None,
    "last_summary": None,
}


def _parse_env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_env_int(name: str, default: int, *, min_value: Optional[int] = None) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except Exception:
            value = default
    if min_value is not None and value < min_value:
        return min_value
    return value


def _csv_env_values(name: str) -> List[str]:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return []
    return [value.strip() for value in raw.split(",") if value.strip()]


_QUEUE_STALL_HINT_SECONDS = _parse_env_int(
    "CALIBRATION_QUEUE_STALL_HINT_SECONDS",
    180,
    min_value=30,
)


def _discover_processed_stems(base_dir: Path) -> Dict[str, List[Path]]:
    found: Dict[str, List[Path]] = {}
    if not base_dir.exists() or not base_dir.is_dir():
        return found

    for slug_dir in sorted(item for item in base_dir.iterdir() if item.is_dir()):
        song_dirs: List[Path] = []
        try:
            for song_dir in sorted(item for item in slug_dir.iterdir() if item.is_dir()):
                if (song_dir / "drum_analysis.json").exists():
                    song_dirs.append(song_dir)
        except Exception:
            continue
        if song_dirs:
            found[slug_dir.name] = song_dirs
    return found


def _run_auto_assimilation_population(
    *,
    base_dir: str,
    max_events_per_stem: int = 5000,
    compute_hashes: bool = False,
    hash_max_bytes: int = 0,
) -> None:
    started_at = datetime.utcnow().isoformat()
    with _AUTO_ASSIMILATION_LOCK:
        _AUTO_ASSIMILATION_STATE["running"] = True
        _AUTO_ASSIMILATION_STATE["last_started_at"] = started_at
        _AUTO_ASSIMILATION_STATE["last_error"] = None

    summary: Dict[str, Any] = {
        "base_dir": base_dir,
        "processed_slugs": [],
        "total_ingested": 0,
        "failed_phases": {},
    }

    try:
        root = Path(base_dir).expanduser().resolve()
        discovered = _discover_processed_stems(root)
        if not discovered:
            raise RuntimeError(f"No song folders with drum_analysis.json found under: {root}")

        effective_slugs = sorted(discovered.keys())

        db = get_db_service()

        for slug in effective_slugs:
            song_dirs = discovered.get(slug) or []
            ingested = 0
            for song_dir in song_dirs:
                try:
                    analysis_id = db.ingest_processed_stems_song_folder(
                        drummer_id=slug,
                        song_folder=str(song_dir),
                        compute_hashes=bool(compute_hashes),
                        hash_max_bytes=int(hash_max_bytes or 0),
                        analysis_version="baseline_v1",
                    )
                    if analysis_id:
                        ingested += 1
                except Exception as exc:
                    logger.warning("Auto-assimilation ingest failed for %s (%s): %s", slug, song_dir, exc)

            summary["total_ingested"] += ingested

            phase_results = {
                "phase2": db.run_phase2_hit_event_extraction_for_drummer(
                    drummer_slug=slug,
                    max_events_per_stem=int(max_events_per_stem),
                ),
                "phase3": db.run_phase3_fills_and_techniques_for_drummer(drummer_slug=slug),
                "phase4": db.run_phase4_microtiming_and_dynamics_for_drummer(drummer_slug=slug),
                "phase5": db.run_phase5_profile_rollup_for_drummer(drummer_slug=slug),
                "phase6": db.run_phase6_persona_preset_export_for_drummer(drummer_slug=slug),
                "phase7": db.run_phase7_assimilation_profiles_for_drummer(drummer_slug=slug),
                "phase32_42": db.run_phase32_42_features_for_drummer(drummer_slug=slug),
            }

            failures: Dict[str, str] = {}
            for phase_name, result in phase_results.items():
                if isinstance(result, dict):
                    if result.get("error"):
                        failures[phase_name] = str(result.get("error"))
                    elif "saved" in result and not bool(result.get("saved")):
                        failures[phase_name] = "saved=False"
            if failures:
                summary["failed_phases"][slug] = failures

            summary["processed_slugs"].append(
                {
                    "slug": slug,
                    "song_dirs": len(song_dirs),
                    "ingested": ingested,
                }
            )

    except Exception as exc:
        logger.exception("Automatic assimilation population failed")
        with _AUTO_ASSIMILATION_LOCK:
            _AUTO_ASSIMILATION_STATE["last_error"] = str(exc)
            _AUTO_ASSIMILATION_STATE["last_summary"] = summary
    else:
        with _AUTO_ASSIMILATION_LOCK:
            _AUTO_ASSIMILATION_STATE["last_summary"] = summary
    finally:
        with _AUTO_ASSIMILATION_LOCK:
            _AUTO_ASSIMILATION_STATE["running"] = False
            _AUTO_ASSIMILATION_STATE["last_completed_at"] = datetime.utcnow().isoformat()


def _start_auto_assimilation_population(
    *,
    base_dir: str,
    max_events_per_stem: int = 5000,
    compute_hashes: bool = False,
    hash_max_bytes: int = 0,
) -> bool:
    with _AUTO_ASSIMILATION_LOCK:
        if bool(_AUTO_ASSIMILATION_STATE.get("running")):
            return False

    worker = threading.Thread(
        target=_run_auto_assimilation_population,
        kwargs={
            "base_dir": base_dir,
            "max_events_per_stem": max_events_per_stem,
            "compute_hashes": compute_hashes,
            "hash_max_bytes": hash_max_bytes,
        },
        daemon=True,
        name="auto-assimilation-populate",
    )
    worker.start()
    return True


def _extract_rollup_parts(rollup_result: Any) -> Dict[str, Any]:
    rollup_payload = rollup_result.get("rollup") if isinstance(rollup_result, dict) else {}
    if not isinstance(rollup_payload, dict):
        rollup_payload = {}

    comparison = rollup_result.get("comparison") if isinstance(rollup_result, dict) else None
    if not isinstance(comparison, dict):
        comparison = rollup_payload.get("comparison") if isinstance(rollup_payload, dict) else None
    if not isinstance(comparison, dict):
        comparison = None

    metrics = rollup_result.get("metrics") if isinstance(rollup_result, dict) else None
    if not isinstance(metrics, dict):
        metrics = rollup_payload.get("metrics") if isinstance(rollup_payload, dict) else None
    if not isinstance(metrics, dict):
        metrics = None

    return {
        "rollup_payload": rollup_payload,
        "comparison": comparison,
        "metrics": metrics,
    }


def _complete_generation_run(*, slug: str, run_id: str) -> None:
    started_at = datetime.utcnow()
    db: Optional[CentralDatabaseService] = None
    rollup_result: Dict[str, Any] = {}
    try:
        db = get_db_service()
        raw_result = db.run_phase5_profile_rollup_for_drummer(drummer_slug=slug) or {}
        if not isinstance(raw_result, dict):
            raise RuntimeError("Phase 5 rollup returned an invalid response")
        rollup_result = raw_result

        if not bool(rollup_result.get("saved")):
            phase7 = rollup_result.get("phase7") if isinstance(rollup_result.get("phase7"), dict) else {}
            failure_detail = (
                rollup_result.get("error")
                or phase7.get("error")
                or "Phase 5 rollup did not save any calibration data"
            )
            raise RuntimeError(str(failure_detail))

        parts = _extract_rollup_parts(rollup_result)
        rollup_payload = parts["rollup_payload"]
        comparison = parts["comparison"]
        metrics = parts["metrics"]

        if not rollup_payload:
            raise RuntimeError("Phase 5 rollup returned an empty payload")

        db.log_calibration_run(
            run_id=run_id,
            drummer_slug=slug,
            outcome="success",
            started_at=started_at,
            completed_at=datetime.utcnow(),
            metadata=rollup_payload,
            metrics=metrics,
            comparison=comparison,
            note_count=rollup_payload.get("note_count") if isinstance(rollup_payload, dict) else None,
            fills_per_minute=rollup_payload.get("fills_per_min") if isinstance(rollup_payload, dict) else None,
            within_tolerance_count=(comparison.get("within_tolerance_count") if isinstance(comparison, dict) else None),
            total_compared=(comparison.get("total_compared") if isinstance(comparison, dict) else None),
        )
    except Exception as exc:
        logger.exception("Calibration generation failed for %s", slug)
        if db is None:
            try:
                db = get_db_service()
            except Exception:
                db = None
        if db is not None:
            try:
                db.log_calibration_run(
                    run_id=run_id,
                    drummer_slug=slug,
                    outcome="failure",
                    started_at=started_at,
                    completed_at=datetime.utcnow(),
                    metadata={
                        "error": str(exc),
                        "saved": bool(rollup_result.get("saved")),
                    },
                )
            except Exception:
                logger.exception("Failed to persist calibration failure run for %s", slug)


def _assert_postgres_database_configured() -> None:
    backend_env = str(os.getenv("DB_BACKEND", "")).strip().lower()
    db_url_env = str(os.getenv("DATABASE_URL", "")).strip()
    if backend_env not in {"postgres", "postgresql"}:
        raise RuntimeError("Calibration API requires DB_BACKEND=postgres")
    if not db_url_env.lower().startswith("postgres"):
        raise RuntimeError("Calibration API requires a valid Postgres DATABASE_URL")


def get_db_service() -> CentralDatabaseService:
    """Return one initialized Postgres-backed CentralDatabaseService instance.

    Avoid spawning a new initialization thread per request. Cold Render starts
    and slow Supabase pool warm-up should become a clear startup/config error,
    not intermittent calibration-page failures.
    """
    global _DB_SERVICE_READY  # noqa: PLW0603

    _assert_postgres_database_configured()
    svc = CentralDatabaseService.get_instance()
    if svc is None:
        raise RuntimeError("CentralDatabaseService unavailable")

    if _DB_SERVICE_READY and getattr(svc, "_engine", None) is not None:
        return svc

    with _DB_INIT_LOCK:
        if _DB_SERVICE_READY and getattr(svc, "_engine", None) is not None:
            return svc
        try:
            ok = bool(svc.initialize())
        except Exception as exc:
            raise RuntimeError(f"CentralDatabaseService failed to initialize: {exc}") from exc
        if not ok:
            raise RuntimeError("CentralDatabaseService failed to initialize")
        if getattr(svc, "_engine", None) is None:
            raise RuntimeError("Calibration API requires Postgres (DB_BACKEND=postgres with valid DATABASE_URL)")
        _DB_SERVICE_READY = True
        return svc


class CompletionStatusInfo(BaseModel):
    status: str
    completion_ratio: Optional[float] = None


class DrummerListItem(BaseModel):
    slug: str
    displayName: str
    completionStatus: CompletionStatusInfo
    assimilationStatus: Optional[Dict[str, Any]] = None
    latestRunAt: Optional[datetime] = None
    metricsWithin: Optional[int] = None
    metricsCompared: Optional[int] = None


class CalibrationRunPayload(BaseModel):
    id: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    outcome: str
    note_count: Optional[int] = None
    fills_per_minute: Optional[float] = None
    delta_summary: Optional[str] = None
    metrics_within: Optional[int] = None
    metrics_compared: Optional[int] = None
    error_message: Optional[str] = None


class FeedbackEntry(BaseModel):
    id: str
    submitted_at: datetime
    author: Optional[str] = None
    rating: int
    comment: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AdjustmentPayload(BaseModel):
    adjustments: Dict[str, Any]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GenerateCandidatesRequest(BaseModel):
    base_groove_id: str
    target_drummer_slug: str
    candidate_count: int = Field(default=2, ge=1, le=4)
    include_baseline: bool = True
    render_profile_id: str = "calibration_standard_v1"
    sample_pack_version: str = "default"
    reviewer_id: Optional[str] = None
    seed: Optional[int] = None
    generation_controls: Optional[Dict[str, Any]] = None
    strict_reference_baseline: bool = False
    wait_for_all_artifacts: bool = False
    artifact_wait_timeout_sec: int = Field(default=120, ge=30, le=600)
    artifact_poll_interval_ms: int = Field(default=1500, ge=500, le=10000)


class AutoAssimilationPopulateRequest(BaseModel):
    base_dir: Optional[str] = None
    max_events_per_stem: int = Field(default=5000, ge=100, le=50000)
    compute_hashes: bool = False
    hash_max_bytes: int = 0


class DrummerGenerationControlsPayload(BaseModel):
    target_drummer_id: str
    personality_amount: float = Field(default=0.75, ge=0.0, le=1.0)
    preserve_original_groove: float = Field(default=0.65, ge=0.0, le=1.0)
    fill_aggression: float = Field(default=0.5, ge=0.0, le=1.0)
    ghost_note_detail: float = Field(default=0.6, ge=0.0, le=1.0)
    cymbal_personality: float = Field(default=0.8, ge=0.0, le=1.0)
    timing_personality: float = Field(default=0.7, ge=0.0, le=1.0)
    velocity_personality: float = Field(default=0.75, ge=0.0, le=1.0)
    physical_realism_strictness: float = Field(default=0.9, ge=0.0, le=1.0)
    section_awareness: bool = True


class RunVersionPayload(BaseModel):
    run_id: str
    generator_version: str
    feature_version: str
    rollup_version: str
    sample_pack_version: str
    seed: int
    commit_hash: Optional[str] = None


class AudioArtifactPayload(BaseModel):
    artifact_id: str
    run_id: Optional[str] = None
    artifact_type: str
    storage_uri: str
    public_url: Optional[str] = None
    duration_sec: Optional[float] = None
    loudness_lufs: Optional[float] = None
    sample_pack_version: Optional[str] = None
    render_recipe: Dict[str, Any] = Field(default_factory=dict)


class EvaluationSessionPayload(BaseModel):
    session_id: str
    reviewer_id: str
    target_drummer_slug: str
    assigned_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    app_version: Optional[str] = None
    notes: Optional[str] = None


class EvaluationItemPayload(BaseModel):
    item_id: str
    session_id: str
    target_drummer_slug: str
    base_groove_id: str
    baseline_label: Optional[str] = None
    baseline_reference_audio_url: Optional[str] = None
    reference_artifact_id: Optional[str] = None
    baseline_run_id: Optional[str] = None
    candidate_a_run_id: Optional[str] = None
    candidate_b_run_id: Optional[str] = None
    eval_mode: Literal["single", "AB", "ABX"] = "AB"
    ab_mapping: Dict[str, Any] = Field(default_factory=dict)
    artifact_map: Dict[str, List[AudioArtifactPayload]] = Field(default_factory=dict)


class PairwiseJudgmentSubmit(BaseModel):
    preferred_candidate: Literal["A", "B", "tie"]
    closer_to_target: Literal["A", "B", "tie"]
    better_feel: Literal["A", "B", "tie"]
    more_musical: Literal["A", "B", "tie"]
    confidence: int = Field(ge=1, le=5)


class AttributeRatingsSubmit(BaseModel):
    candidate_label: Literal["A", "B", "single"]
    stylistic_authenticity: float = Field(ge=1, le=10)
    groove_feel: float = Field(ge=1, le=10)
    dynamics: float = Field(ge=1, le=10)
    phrasing: float = Field(ge=1, le=10)
    kit_balance: float = Field(ge=1, le=10)
    fill_behavior: float = Field(ge=1, le=10)
    human_realism: float = Field(ge=1, le=10)
    overall_usefulness: float = Field(ge=1, le=10)


class DrummerDetailPayload(BaseModel):
    slug: str
    displayName: str
    adjustments: Dict[str, Any]
    rollupTargets: Dict[str, Any]
    metrics: Dict[str, Any]
    metadata: Dict[str, Any]
    assimilationStatus: Optional[Dict[str, Any]] = None
    runHistory: Optional[List[CalibrationRunPayload]] = None
    feedbackSamples: Optional[List[FeedbackEntry]] = None
    completionStatus: Optional[CompletionStatusInfo] = None


class CalibrationHealthPayload(BaseModel):
    status: str
    db_path: Optional[str] = None
    db_exists: bool = False
    calibration_tables: Dict[str, bool] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class CalibrationTrainingExportPayload(BaseModel):
    exported_at: datetime
    item_count: int
    filters: Dict[str, Any] = Field(default_factory=dict)
    items: List[Dict[str, Any]] = Field(default_factory=list)


class StoragePresignUploadRequest(BaseModel):
    drummer_slug: str
    run_id: Optional[str] = None
    file_name: str
    content_type: Optional[str] = None


class StoragePresignUploadResponse(BaseModel):
    bucket: str
    key: str
    url: str
    fields: Dict[str, Any]
    expires_in: int


class StoragePresignDownloadRequest(BaseModel):
    drummer_slug: str
    key: str


class StoragePresignDownloadResponse(BaseModel):
    bucket: str
    key: str
    url: str
    expires_in: int


class AnalysisJobCreateRequest(BaseModel):
    drummer_slug: str
    input_json: Dict[str, Any] = Field(default_factory=dict)


class AnalysisJobResponse(BaseModel):
    id: str
    drummer_id: str
    status: str
    input_json: Optional[str] = None
    result_json: Optional[str] = None
    error_text: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


def _bearer_token_from_request(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _parse_jwt_sub_unverified(token: str) -> Optional[str]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + ("=" * ((4 - len(parts[1]) % 4) % 4))
        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("utf-8"))
        data = json.loads(payload_bytes.decode("utf-8"))
        sub = data.get("sub") or data.get("user_id")
        return str(sub) if sub else None
    except Exception:
        return None


_JWKS_CACHE: Dict[str, Any] = {"keys": [], "fetched_at": 0.0}


def _load_jwks(force: bool = False) -> Dict[str, Any]:
    import time as _time
    now = _time.time()
    ttl = 600.0
    if not force and _JWKS_CACHE.get("keys") and (now - float(_JWKS_CACHE.get("fetched_at", 0.0))) < ttl:
        return _JWKS_CACHE
    url = os.getenv("SUPABASE_JWKS_URL", "").strip()
    if not url:
        return {"keys": []}
    try:
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            _JWKS_CACHE["keys"] = data["keys"]
            _JWKS_CACHE["fetched_at"] = now
            return _JWKS_CACHE
    except Exception:
        return {"keys": []}
    return {"keys": []}


def _verify_supabase_jwt(token: str) -> Optional[Dict[str, Any]]:
    audience = os.getenv("SUPABASE_JWT_AUDIENCE", None)
    jwks = _load_jwks()
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            try:
                return jwt.decode(token, key, algorithms=[key.get("alg", "RS256")], audience=audience)
            except Exception:
                continue
    # As a fallback, try each key
    for key in jwks.get("keys", []):
        try:
            return jwt.decode(token, key, algorithms=[key.get("alg", "RS256")], audience=audience)
        except Exception:
            continue
    return None


def _require_user(request: Request) -> str:
    token = _bearer_token_from_request(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    # Production: verify against Supabase JWKS
    if str(os.getenv("ALLOW_UNVERIFIED_JWT", "").strip().lower()) in {"1", "true", "yes"}:
        user_id = _parse_jwt_sub_unverified(token)
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return user_id
    claims = _verify_supabase_jwt(token)
    if not isinstance(claims, dict):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token verification failed")
    sub = str(claims.get("sub") or claims.get("user_id") or "").strip()
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing subject")
    return sub


class AnalysisDetailPayload(BaseModel):
    analysis_id: str
    drummer_slug: Optional[str] = None
    song_title: Optional[str] = None
    source_file: Optional[str] = None
    tempo_bpm: Optional[float] = None
    time_signature: Optional[str] = None
    duration_sec: Optional[float] = None
    created_at: Optional[str] = None
    hit_event_count: Optional[int] = None


class FeedbackSubmitRequest(BaseModel):
    drummer: str
    run_id: Optional[str] = None
    item_id: Optional[str] = None
    rating: int = Field(..., ge=1, le=5)
    comment: str
    author: Optional[str] = Field(default="Guest")


def _slug_from_row(row: Dict[str, Any]) -> str:
    for key in ("slug", "drummer_slug", "drummer_id", "id"):
        value = row.get(key)
        if value:
            slug = str(value).strip()
            if slug:
                return slug
    return ""


def _display_name_from_row(row: Dict[str, Any], slug: str) -> str:
    for key in ("display_name", "displayName", "name", "drummer_name"):
        value = row.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    if not slug:
        return "Unknown Drummer"
    return " ".join(part.capitalize() for part in slug.replace("-", "_").split("_") if part) or slug


def _completion_from_counts(within: Optional[int], total: Optional[int]) -> CompletionStatusInfo:
    if not total or total <= 0 or within is None:
        return CompletionStatusInfo(status="unknown", completion_ratio=None)
    ratio = float(within) / float(total)
    if ratio >= 0.8:
        status = "ready"
    elif ratio >= 0.6:
        status = "refine"
    else:
        status = "needs_tuning"
    return CompletionStatusInfo(status=status, completion_ratio=ratio)


def _completion_from_run(run: Optional["CalibrationRun"]) -> CompletionStatusInfo:
    if not run:
        return CompletionStatusInfo(status="unknown", completion_ratio=None)
    return _completion_from_counts(run.within_tolerance_count, run.total_compared)


def _require_postgres_engine(db: CentralDatabaseService):
    engine = getattr(db, "_engine", None)
    if engine is None:
        raise RuntimeError("Calibration API requires Postgres engine")
    return engine


def _assimilation_status_for_slug(db: CentralDatabaseService, slug: str) -> Dict[str, Any]:
    slug = (slug or "").strip()
    status: Dict[str, Any] = {
        "status": "unknown",
        "ready_for_calibration": False,
        "missing_steps": ["ingestion"],
        "counts": {
            "songs": 0,
            "artifacts": 0,
            "stems": 0,
            "hit_events": 0,
            "fills": 0,
            "techniques": 0,
        },
        "metrics": {
            "phase4_enriched_analyses": 0,
            "phase5_rollups": 0,
            "phase6_presets": 0,
        },
    }
    if not slug:
        return status

    engine = _require_postgres_engine(db)

    def _safe_count_pg(sql: str, params: Dict[str, Any]) -> int:
        try:
            with engine.connect() as conn_pg:
                row = conn_pg.execute(text(sql), params).first()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0

    songs = _safe_count_pg(
        """
        SELECT COUNT(DISTINCT analysis_id)
        FROM public.song_performance_analysis
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    artifacts = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.analysis_artifacts
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    stems = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.stem_artifacts
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    hit_events = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.drum_hit_events
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    fills = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.fill_events
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    techniques = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.technique_events
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    phase4_enriched = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.song_performance_analysis
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
          AND groove_micro_timing_variance IS NOT NULL
          AND groove_pocket_tightness IS NOT NULL
          AND humanness_score IS NOT NULL
        """,
        {"slug": slug},
    )
    rollup_count = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.drummer_profile_rollups
        WHERE CAST(drummer_id AS TEXT) = CAST(:slug AS TEXT)
        """,
        {"slug": slug},
    )
    preset_count = _safe_count_pg(
        """
        SELECT COUNT(1)
        FROM public.drummer_presets
        WHERE (profile_type = 'drummer' AND CAST(source_ref AS TEXT) = CAST(:slug AS TEXT))
           OR CAST(preset_id AS TEXT) = CAST(:preset_id AS TEXT)
        """,
        {"slug": slug, "preset_id": f"phase6_{slug}"},
    )

    missing_steps: List[str] = []
    if songs <= 0:
        missing_steps.append("ingestion")
    if hit_events <= 0:
        missing_steps.append("phase2_hit_events")
    if fills <= 0 or techniques <= 0:
        missing_steps.append("phase3_fills_techniques")
    if phase4_enriched <= 0:
        missing_steps.append("phase4_microtiming_dynamics")
    if rollup_count <= 0:
        missing_steps.append("phase5_rollup")
    if preset_count <= 0:
        missing_steps.append("phase6_persona_preset")

    has_downstream_assimilation = (
        phase4_enriched > 0
        and rollup_count > 0
        and preset_count > 0
    )
    if has_downstream_assimilation:
        missing_steps = [
            step
            for step in missing_steps
            if step not in {"phase2_hit_events", "phase3_fills_techniques"}
        ]

    ready = len(missing_steps) == 0
    overall_status = "ready_for_calibration" if ready else "needs_processing"

    status["status"] = overall_status
    status["ready_for_calibration"] = ready
    status["missing_steps"] = missing_steps
    status["counts"] = {
        "songs": songs,
        "artifacts": artifacts,
        "stems": stems,
        "hit_events": hit_events,
        "fills": fills,
        "techniques": techniques,
    }
    status["metrics"] = {
        "phase4_enriched_analyses": phase4_enriched,
        "phase5_rollups": rollup_count,
        "phase6_presets": preset_count,
    }
    return status


def _serialize_run(run: "CalibrationRun") -> CalibrationRunPayload:
    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    error_message = metadata.get("error") if isinstance(metadata, dict) else None
    if not error_message and run.outcome == "failure":
        error_message = run.delta_summary

    return CalibrationRunPayload(
        id=run.run_id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        outcome=run.outcome,
        note_count=run.note_count,
        fills_per_minute=run.fills_per_minute,
        delta_summary=run.delta_summary,
        metrics_within=run.within_tolerance_count,
        metrics_compared=run.total_compared,
        error_message=str(error_message) if error_message else None,
    )


def _serialize_feedback(entry: "CalibrationFeedback") -> FeedbackEntry:
    return FeedbackEntry(
        id=entry.feedback_id,
        submitted_at=entry.submitted_at,
        author=entry.author,
        rating=entry.rating,
        comment=entry.comment,
        metadata=_safe_json_dict(getattr(entry, "metadata", None)),
    )


def _safe_json_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _model_dump(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()  # type: ignore[attr-defined]
    return model.dict()


def _serialize_run_version(version: Optional["RunVersion"]) -> Optional[RunVersionPayload]:
    if not version:
        return None
    return RunVersionPayload(
        run_id=version.run_id,
        generator_version=version.generator_version,
        feature_version=version.feature_version,
        rollup_version=version.rollup_version,
        sample_pack_version=version.sample_pack_version,
        seed=version.seed,
        commit_hash=version.commit_hash,
    )


def _serialize_artifact(artifact: "AudioArtifact") -> AudioArtifactPayload:
    run_id = str(artifact.run_id or "").strip() if artifact.run_id is not None else ""
    artifact_id = str(artifact.artifact_id or "").strip()
    if run_id and artifact_id:
        public_url = f"/calibration/audio-artifacts/{quote(run_id)}/{quote(artifact_id)}"
    else:
        public_url = _artifact_url_service.build_url(artifact.storage_uri)
    return AudioArtifactPayload(
        artifact_id=artifact.artifact_id,
        run_id=artifact.run_id,
        artifact_type=artifact.artifact_type,
        storage_uri=artifact.storage_uri,
        public_url=public_url,
        duration_sec=artifact.duration_sec,
        loudness_lufs=artifact.loudness_lufs,
        sample_pack_version=artifact.sample_pack_version,
        render_recipe=getattr(artifact, "render_recipe", {}) or {},
    )


def _serialize_session(session: "EvaluationSession") -> EvaluationSessionPayload:
    return EvaluationSessionPayload(
        session_id=session.session_id,
        reviewer_id=session.reviewer_id,
        target_drummer_slug=session.target_drummer_slug,
        assigned_at=session.assigned_at,
        started_at=session.started_at,
        completed_at=session.completed_at,
        app_version=session.app_version,
        notes=session.notes,
    )


def _serialize_item(
    item: "EvaluationItem",
    artifact_lookup: Dict[str, List[AudioArtifactPayload]],
    *,
    baseline_label: Optional[str] = None,
    baseline_reference_audio_url: Optional[str] = None,
) -> EvaluationItemPayload:
    eval_mode = item.eval_mode if item.eval_mode in {"single", "AB", "ABX"} else "AB"
    return EvaluationItemPayload(
        item_id=item.item_id,
        session_id=item.session_id,
        target_drummer_slug=item.target_drummer_slug,
        base_groove_id=item.base_groove_id,
        baseline_label=baseline_label,
        baseline_reference_audio_url=baseline_reference_audio_url,
        reference_artifact_id=item.reference_artifact_id,
        baseline_run_id=item.baseline_run_id,
        candidate_a_run_id=item.candidate_a_run_id,
        candidate_b_run_id=item.candidate_b_run_id,
        eval_mode=eval_mode,
        ab_mapping=item.ab_mapping or {},
        artifact_map=artifact_lookup,
    )


def _pick_reference_artifact_id(
    db: CentralDatabaseService,
    *,
    baseline_run_id: Optional[str],
) -> Optional[str]:
    run_id = (baseline_run_id or "").strip()
    if not run_id:
        return None
    try:
        artifacts = db.get_audio_artifacts_for_run(run_id=run_id)
    except Exception:
        return None
    if not artifacts:
        return None

    preferred = [
        artifact
        for artifact in artifacts
        if str(getattr(artifact, "artifact_type", "")).lower() in {"mix", "preview", "audio"}
    ]
    selected = preferred[0] if preferred else artifacts[0]
    return str(getattr(selected, "artifact_id", "") or "") or None


def _resolve_local_path(value: Any) -> Optional[Path]:
    text = str(value or "").strip()
    if not text:
        return None
    lower = text.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return None
    candidate = Path(text)
    if candidate.is_file():
        return candidate.resolve()
    root = Path(__file__).resolve().parents[1]
    trimmed = text.lstrip("/\\")
    if trimmed and trimmed != text:
        rooted = (root / trimmed).resolve()
        if rooted.is_file():
            return rooted
    alt = (root / candidate).resolve()
    if alt.is_file():
        return alt
    return None


def _coerce_tempo_bpm(value: Any, default: float = 110.0) -> float:
    try:
        bpm = float(value)
        if bpm <= 0:
            return float(default)
        return bpm
    except Exception:
        return float(default)


def _repair_legacy_preview_artifact_if_missing(
    db: CentralDatabaseService,
    *,
    run_id: str,
    artifact: "AudioArtifact",
) -> Optional[Path]:
    run_id_val = str(run_id or "").strip()
    if not run_id_val:
        return None

    storage_uri = str(getattr(artifact, "storage_uri", "") or "").strip()
    if not storage_uri:
        return None

    normalized = storage_uri.replace("\\", "/").lower()
    expected_suffix = f"/candidates/{run_id_val.lower()}/preview.wav"
    if expected_suffix not in normalized:
        return None

    existing = _resolve_local_path(storage_uri)
    if existing and existing.is_file():
        return existing

    tempo_bpm = 110.0
    try:
        run_ref = db.get_calibration_run(run_id=run_id_val)
        run_meta = run_ref.metadata if run_ref and isinstance(run_ref.metadata, dict) else {}
        if isinstance(run_meta, dict):
            render_meta = run_meta.get("render") if isinstance(run_meta.get("render"), dict) else {}
            tempo_bpm = _coerce_tempo_bpm(
                run_meta.get("tempo_bpm", render_meta.get("tempo_bpm")),
                default=110.0,
            )
    except Exception:
        tempo_bpm = 110.0

    try:
        render_service = CalibrationRenderService(db)
        repaired_path = render_service._synthesize_preview_audio(run_id=run_id_val, tempo_bpm=tempo_bpm)
        if not repaired_path or not repaired_path.is_file():
            return None

        artifact_id_val = str(getattr(artifact, "artifact_id", "") or "").strip() or None
        artifact_type_val = str(getattr(artifact, "artifact_type", "audio") or "audio").strip() or "audio"
        recipe = getattr(artifact, "render_recipe", {})
        if not isinstance(recipe, dict):
            recipe = {}

        db.log_audio_artifact(
            run_id=run_id_val,
            artifact_type=artifact_type_val,
            storage_uri=str(repaired_path.resolve()),
            duration_sec=getattr(artifact, "duration_sec", None),
            loudness_lufs=getattr(artifact, "loudness_lufs", None),
            sample_pack_version=getattr(artifact, "sample_pack_version", None),
            render_recipe=recipe,
            artifact_id=artifact_id_val,
        )

        logger.info(
            "legacy_preview_artifact_repaired run_id=%s artifact_id=%s storage_uri=%s",
            run_id_val,
            artifact_id_val,
            str(repaired_path),
        )
        return repaired_path
    except Exception:
        logger.warning(
            "legacy_preview_artifact_repair_failed run_id=%s artifact_id=%s",
            run_id_val,
            str(getattr(artifact, "artifact_id", "") or "").strip() or None,
            exc_info=True,
        )
        return None


def _song_label_from_path(path: Path) -> str:
    parent = path.parent
    parent_name = parent.name.strip()
    generic_dirs = {"drumsep_components", "components", "stems"}
    if parent_name.lower() in generic_dirs and parent.parent:
        parent_name = parent.parent.name.strip()
    stem = path.stem.strip()
    label = parent_name or stem or "Assimilated Reference"
    return label.replace("_", " ")


def _song_label_from_uri(uri: str) -> str:
    text = str(uri or "").strip()
    if not text:
        return "Assimilated Reference"
    parsed = urlparse(text)
    candidate = parsed.path if parsed.scheme else text
    stem = Path(candidate).stem.strip()
    label = stem or Path(candidate).name.strip() or "Assimilated Reference"
    return label.replace("_", " ")


def _instrument_id_from_hit(instrument: str, component: str) -> str:
    inst = str(instrument or "").strip().lower()
    comp = str(component or "").strip().lower().replace(" ", "_")
    if comp and comp not in {"none", "null"}:
        if comp.startswith(inst):
            return comp
        if inst:
            return f"{inst}_{comp}"
    mapping = {
        "kick": "kick",
        "snare": "snare_center",
        "hihat": "hihat_closed",
        "hh": "hihat_closed",
        "ride": "ride_bow",
        "crash": "crash",
        "tom": "tom_mid",
        "toms": "tom_mid",
    }
    return mapping.get(inst, inst or "kick")


def _normalize_storage_uri(value: Any) -> str:
    return str(value or "").strip()


def _is_cloud_readable_storage_uri(value: Any) -> bool:
    uri = _normalize_storage_uri(value)
    if not uri:
        return False
    lower = uri.lower()
    return lower.startswith(("https://", "http://", "s3://", "supabase://", "gs://", "r2://"))


_AUDIO_FILE_SUFFIXES = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".m4a",
    ".aac",
    ".aif",
    ".aiff",
    ".wma",
}
_NON_AUDIO_FILE_SUFFIXES = {
    ".json",
    ".txt",
    ".csv",
    ".xml",
    ".yaml",
    ".yml",
    ".md",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".mid",
    ".midi",
}


def _is_likely_audio_storage_uri(value: Any) -> bool:
    uri = _normalize_storage_uri(value)
    if not uri:
        return False

    try:
        parsed = urlparse(uri)
        path_value = parsed.path or uri
    except Exception:
        path_value = uri

    suffix = Path(path_value).suffix.lower()
    if suffix in _AUDIO_FILE_SUFFIXES:
        return True
    if suffix in _NON_AUDIO_FILE_SUFFIXES:
        return False
    return True


def _baseline_source_summary(source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(source, dict):
        return {"present": False}

    summary: Dict[str, Any] = {
        "present": True,
        "analysis_id": str(source.get("analysis_id") or "").strip() or None,
        "has_base_groove_path": bool(str(source.get("base_groove_path") or "").strip()),
        "has_source_path": bool(str(source.get("source_path") or "").strip()),
        "has_source_uri": bool(str(source.get("source_uri") or "").strip()),
        "keys": sorted(str(key) for key in source.keys()),
    }

    for key in (
        "storage_uri",
        "audio_storage_uri",
        "artifact_storage_uri",
        "source_storage_uri",
        "s3_uri",
        "audio_s3_uri",
        "public_url",
        "audio_url",
        "source_uri",
        "source_path",
        "base_groove_path",
    ):
        value = str(source.get(key) or "").strip()
        if value:
            summary[f"{key}_kind"] = "cloud" if _is_cloud_readable_storage_uri(value) else "local_or_relative"

    return summary


def _reference_storage_uri_from_baseline_source(source: Dict[str, Any]) -> Optional[str]:
    for key in (
        "storage_uri",
        "audio_storage_uri",
        "artifact_storage_uri",
        "source_storage_uri",
        "s3_uri",
        "audio_s3_uri",
        "public_url",
        "audio_url",
        "source_uri",
        "source_path",
        "base_groove_path",
    ):
        value = _normalize_storage_uri(source.get(key))
        if _is_cloud_readable_storage_uri(value) and _is_likely_audio_storage_uri(value):
            return value
    return None


def _stable_reference_id_part(*, drummer_slug: str, analysis_id: str, storage_uri: str) -> str:
    basis = "|".join([
        drummer_slug.strip(),
        analysis_id.strip(),
        storage_uri.strip(),
    ])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _find_existing_reference_baseline(
    db: CentralDatabaseService,
    *,
    drummer_slug: str,
    analysis_id: str,
    fingerprint: str,
) -> Optional[Dict[str, str]]:
    expected_run_id = f"baseline-ref-{fingerprint}"
    try:
        engine = _require_postgres_engine(db)
        with engine.connect() as conn_pg:
            row = conn_pg.execute(
                text(
                    """
                    SELECT r.run_id, a.artifact_id
                    FROM public.calibration_runs r
                    JOIN public.audio_artifacts a ON a.run_id = r.run_id
                    WHERE r.drummer_slug = :drummer_slug
                      AND r.run_id = :run_id
                    ORDER BY r.started_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "drummer_slug": drummer_slug,
                    "run_id": expected_run_id,
                },
            ).mappings().first()
        if not row:
            return None
        return {
            "run_id": str(row["run_id"]),
            "artifact_id": str(row["artifact_id"]),
        }
    except Exception:
        logger.warning(
            "baseline_reference_lookup_failed drummer=%s analysis_id=%s run_id=%s",
            drummer_slug,
            analysis_id,
            expected_run_id,
            exc_info=True,
        )
        return None


def _ensure_reference_baseline_run(
    db: CentralDatabaseService,
    *,
    drummer_slug: str,
    baseline_source: Dict[str, Any],
    base_groove_id: str,
    sample_pack_version: Optional[str] = None,
) -> Dict[str, str]:
    analysis_id = str(baseline_source.get("analysis_id") or "").strip()
    source_song_name = str(baseline_source.get("source_song_name") or "").strip() or (analysis_id or "Assimilated Reference")
    storage_uri = _reference_storage_uri_from_baseline_source(baseline_source)
    if not storage_uri:
        raise RuntimeError(
            "Selected assimilation baseline source has no cloud-readable storage URI. "
            "Backfill a source clip URL into storage before queueing strict baseline mode."
        )

    fingerprint = _stable_reference_id_part(
        drummer_slug=drummer_slug,
        analysis_id=analysis_id,
        storage_uri=storage_uri,
    )

    existing = _find_existing_reference_baseline(
        db,
        drummer_slug=drummer_slug,
        analysis_id=analysis_id,
        fingerprint=fingerprint,
    )
    if existing:
        return {
            "run_id": existing["run_id"],
            "artifact_id": existing["artifact_id"],
            "baseline_label": source_song_name,
        }

    run_id = f"baseline-ref-{fingerprint}"
    artifact_id = f"artifact-{run_id}"
    metadata = {
        "requested_via": "baseline_reference",
        "source_type": "assimilated_song",
        "source_song_name": source_song_name,
        "source_analysis_id": analysis_id,
        "source_fingerprint": fingerprint,
        "source_storage_uri": storage_uri,
        "target_drummer_slug": drummer_slug,
        "base_groove_id": base_groove_id,
    }
    logged_run_id = db.log_calibration_run(
        drummer_slug=drummer_slug,
        outcome="reference",
        note_count=None,
        metadata=metadata,
        metrics={},
        comparison={},
        run_id=run_id,
    )
    if not logged_run_id:
        raise RuntimeError("Failed to upsert strict baseline reference run")

    render_recipe = {
        "requested_via": "baseline_reference",
        "source_type": "assimilated_song",
        "source_song_name": source_song_name,
        "analysis_id": analysis_id,
        "target_drummer_slug": drummer_slug,
        "base_groove_id": base_groove_id,
        "source_storage_uri": storage_uri,
    }
    logged_artifact_id = db.log_audio_artifact(
        run_id=logged_run_id,
        artifact_type="reference_song",
        storage_uri=storage_uri,
        duration_sec=None,
        loudness_lufs=None,
        sample_pack_version=sample_pack_version,
        render_recipe=render_recipe,
        artifact_id=artifact_id,
    )
    if not logged_artifact_id:
        raise RuntimeError("Failed to upsert strict baseline reference artifact")

    return {
        "run_id": logged_run_id,
        "artifact_id": logged_artifact_id,
        "baseline_label": source_song_name,
    }


def _build_assimilation_base_groove(
    db: CentralDatabaseService,
    *,
    drummer_slug: str,
    analysis_id: str,
) -> Optional[Path]:
    engine = _require_postgres_engine(db)
    with engine.connect() as conn_pg:
        spa = conn_pg.execute(
            text(
                """
                SELECT tempo_bpm, time_signature
                FROM public.song_performance_analysis
                WHERE analysis_id = :analysis_id
                LIMIT 1
                """
            ),
            {"analysis_id": analysis_id},
        ).mappings().first()
        rows = conn_pg.execute(
            text(
                """
                SELECT instrument, component, onset_time_sec, velocity_est, bar_index
                FROM public.drum_hit_events
                WHERE analysis_id = :analysis_id
                ORDER BY onset_time_sec ASC
                """
            ),
            {"analysis_id": analysis_id},
        ).mappings().all()
    if not spa:
        return None

    try:
        tempo_bpm = float(spa["tempo_bpm"] or 110.0)
    except Exception:
        tempo_bpm = 110.0
    time_signature = str(spa["time_signature"] or "4/4")
    try:
        beats_per_bar = int(time_signature.split("/", 1)[0]) if "/" in time_signature else 4
    except Exception:
        beats_per_bar = 4
    sec_per_bar = (60.0 / max(1e-6, tempo_bpm)) * max(1, beats_per_bar)

    if not rows:
        return None

    first_onset = None
    raw_events: List[Dict[str, Any]] = []
    min_bar_idx = None
    for row in rows:
        try:
            onset = float(row["onset_time_sec"])
        except Exception:
            continue
        if first_onset is None:
            first_onset = onset
        try:
            bar_idx = int(row["bar_index"]) if row["bar_index"] is not None else None
        except Exception:
            bar_idx = None
        if bar_idx is not None:
            min_bar_idx = bar_idx if min_bar_idx is None else min(min_bar_idx, bar_idx)
        try:
            velocity = int(round(float(row["velocity_est"] or 90.0)))
        except Exception:
            velocity = 90
        raw_events.append(
            {
                "instrument": str(row["instrument"] or ""),
                "component": str(row["component"] or ""),
                "onset": onset,
                "bar_idx": bar_idx,
                "velocity": max(1, min(127, velocity)),
            }
        )
    if not raw_events or first_onset is None:
        return None

    max_bars = 2
    pattern_events: List[Dict[str, Any]] = []
    for event in raw_events:
        onset_norm = float(event["onset"]) - float(first_onset)
        if event["bar_idx"] is not None and min_bar_idx is not None:
            bar_idx = int(event["bar_idx"]) - int(min_bar_idx)
        else:
            bar_idx = int(onset_norm // sec_per_bar)
        if bar_idx < 0 or bar_idx >= max_bars:
            continue
        bar_start = float(bar_idx) * sec_per_bar
        bar_end = bar_start + sec_per_bar
        bar_pos_frac = (onset_norm - bar_start) / sec_per_bar if sec_per_bar > 0 else 0.0
        bar_pos_frac = max(0.0, min(0.999999, bar_pos_frac))
        pattern_events.append(
            {
                "barIndex": int(bar_idx),
                "barStartTime": round(bar_start, 6),
                "barEndTime": round(bar_end, 6),
                "bar_pos_frac": round(bar_pos_frac, 6),
                "time_sec": round(onset_norm, 6),
                "instrument_id": _instrument_id_from_hit(event["instrument"], event["component"]),
                "velocity": int(event["velocity"]),
            }
        )

    if not pattern_events:
        return None

    root = Path(__file__).resolve().parents[1]
    out_dir = root / "artifacts" / "calibration" / "base_grooves" / drummer_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{analysis_id}.json"
    payload = {
        "description": f"Assimilated baseline groove from analysis {analysis_id}",
        "tempo_bpm": tempo_bpm,
        "time_signature": time_signature,
        "ppqn": 960,
        "pattern_events": pattern_events,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def _select_assimilation_baseline_source(
    db: CentralDatabaseService,
    *,
    drummer_slug: str,
    require_cloud_uri: bool = False,
) -> Optional[Dict[str, Any]]:
    engine = _require_postgres_engine(db)
    with engine.connect() as conn_pg:
        analyses = conn_pg.execute(
            text(
                """
                SELECT spa.analysis_id, spa.created_at, spa.source_file, s.title AS song_title
                FROM public.song_performance_analysis spa
                LEFT JOIN public.songs s ON s.id = spa.song_id
                LEFT JOIN public.drummers d ON CAST(d.id AS TEXT) = CAST(spa.drummer_id AS TEXT)
                WHERE CAST(spa.drummer_id AS TEXT) = CAST(:slug AS TEXT)
                   OR CAST(COALESCE(d.drummer_id, '') AS TEXT) = CAST(:slug AS TEXT)
                   OR LOWER(REPLACE(COALESCE(d.display_name, ''), ' ', '_')) = LOWER(CAST(:slug AS TEXT))
                   OR LOWER(REPLACE(COALESCE(d.name, ''), ' ', '_')) = LOWER(CAST(:slug AS TEXT))
                ORDER BY spa.created_at DESC
                LIMIT 50
                """
            ),
            {"slug": drummer_slug},
        ).mappings().all()
    if not analyses:
        return None

    preferred_stems = {"drums", "drum"}
    best_noncloud_candidate: Optional[Dict[str, Any]] = None
    for row in analyses:
        analysis_id = str(row["analysis_id"] or "").strip()
        if not analysis_id:
            continue

        source_path: Optional[Path] = None
        source_uri: Optional[str] = None
        source_song_name: Optional[str] = None

        with engine.connect() as conn_pg:
            stem_rows = conn_pg.execute(
                text(
                    """
                    SELECT stem_name, file_path
                    FROM public.stem_artifacts
                    WHERE analysis_id = :analysis_id
                    """
                ),
                {"analysis_id": analysis_id},
            ).mappings().all()

            analysis_artifact_rows = conn_pg.execute(
                text(
                    """
                    SELECT artifact_role, file_path
                    FROM public.analysis_artifacts
                    WHERE analysis_id = :analysis_id
                    ORDER BY created_at DESC
                    """
                ),
                {"analysis_id": analysis_id},
            ).mappings().all()
        best_stem: Optional[Path] = None
        best_stem_uri: Optional[str] = None
        fallback_stem: Optional[Path] = None
        fallback_stem_uri: Optional[str] = None
        for stem in stem_rows:
            stem_name = str(stem["stem_name"] or "").strip().lower()
            file_path_value = str(stem["file_path"] or "").strip()
            resolved = _resolve_local_path(file_path_value)
            if not resolved and not file_path_value:
                continue
            if stem_name in preferred_stems:
                best_stem = resolved
                best_stem_uri = file_path_value or None
                break
            if fallback_stem is None:
                fallback_stem = resolved
                fallback_stem_uri = file_path_value or None
        source_path = best_stem or fallback_stem
        source_uri = best_stem_uri or fallback_stem_uri

        if source_path is None:
            row_source = str(row["source_file"] or "").strip()
            source_path = _resolve_local_path(row_source)
            if not source_uri and row_source and _is_likely_audio_storage_uri(row_source):
                source_uri = row_source
        elif not source_uri and _is_likely_audio_storage_uri(source_path):
            source_uri = str(source_path)

        if not source_uri and analysis_artifact_rows:
            cloud_candidate: Optional[str] = None
            fallback_candidate: Optional[str] = None
            role_priority = {
                "source_audio": 0,
                "source": 1,
                "drums": 2,
                "drum_mix": 3,
                "mix": 4,
            }
            sorted_rows = sorted(
                analysis_artifact_rows,
                key=lambda item: role_priority.get(str(item.get("artifact_role") or "").strip().lower(), 99),
            )
            for artifact in sorted_rows:
                file_path_value = str(artifact.get("file_path") or "").strip()
                if not file_path_value:
                    continue
                if _is_cloud_readable_storage_uri(file_path_value) and _is_likely_audio_storage_uri(file_path_value):
                    cloud_candidate = file_path_value
                    break
                if fallback_candidate is None and _is_likely_audio_storage_uri(file_path_value):
                    fallback_candidate = file_path_value
            source_uri = cloud_candidate or fallback_candidate or source_uri
            if source_path is None and source_uri:
                source_path = _resolve_local_path(source_uri)

        title = str(row["song_title"] or "").strip()
        base_groove_path = _build_assimilation_base_groove(
            db,
            drummer_slug=drummer_slug,
            analysis_id=analysis_id,
        )

        source_song_name = title
        if not source_song_name and source_path is not None:
            source_song_name = _song_label_from_path(source_path)
        if not source_song_name and source_uri:
            source_song_name = _song_label_from_uri(source_uri)
        if not source_song_name:
            source_song_name = analysis_id

        candidate = {
            "analysis_id": analysis_id,
            "source_path": source_path,
            "source_uri": source_uri,
            "source_song_name": source_song_name,
            "base_groove_path": str(base_groove_path) if base_groove_path else None,
        }

        has_any_source = bool(source_path is not None or str(source_uri or "").strip())
        if not has_any_source:
            continue

        if _is_cloud_readable_storage_uri(source_uri):
            return candidate

        if best_noncloud_candidate is None:
            best_noncloud_candidate = candidate

    if require_cloud_uri:
        return None

    return best_noncloud_candidate


def _create_reference_baseline_run(
    db: CentralDatabaseService,
    *,
    drummer_slug: str,
    baseline_source: Dict[str, Any],
    base_groove_id: str,
) -> Optional[Dict[str, Any]]:
    try:
        return _ensure_reference_baseline_run(
            db,
            drummer_slug=drummer_slug,
            baseline_source=baseline_source,
            base_groove_id=base_groove_id,
            sample_pack_version=None,
        )
    except Exception:
        return None


def _infer_baseline_label(
    *,
    item: "EvaluationItem",
    artifact_lookup: Dict[str, List[AudioArtifactPayload]],
) -> Optional[str]:
    baseline_artifacts = artifact_lookup.get("baseline") or []
    for artifact in baseline_artifacts:
        recipe = artifact.render_recipe or {}
        source_song = str(recipe.get("source_song_name") or "").strip()
        if source_song:
            return source_song
    for lane in ("A", "B"):
        for artifact in artifact_lookup.get(lane) or []:
            recipe = artifact.render_recipe or {}
            source_song = str(recipe.get("source_song_name") or "").strip()
            if source_song:
                return source_song
    return None


def _infer_source_analysis_id(
    *,
    item: "EvaluationItem",
    artifact_lookup: Dict[str, List[AudioArtifactPayload]],
) -> Optional[str]:
    for lane in ("A", "B", "baseline"):
        for artifact in artifact_lookup.get(lane) or []:
            recipe = artifact.render_recipe or {}
            value = str(recipe.get("source_analysis_id") or recipe.get("analysis_id") or "").strip()
            if value:
                return value

    for run_id in (item.candidate_a_run_id, item.candidate_b_run_id, item.baseline_run_id):
        run_id_val = str(run_id or "").strip()
        if not run_id_val:
            continue
        try:
            run_record = CentralDatabaseService.get_instance().get_calibration_run(run_id=run_id_val)
            run_meta = run_record.metadata if run_record and isinstance(run_record.metadata, dict) else {}
            value = str(run_meta.get("source_analysis_id") or "").strip()
            if value:
                return value
        except Exception:
            continue

    return None


def _baseline_reference_audio_url_from_analysis_id(analysis_id: Optional[str]) -> Optional[str]:
    analysis_id_val = str(analysis_id or "").strip()
    if not analysis_id_val:
        return None
    return f"/calibration/analysis/{quote(analysis_id_val)}/reference-audio"


def _first_http_url(*values: Any) -> Optional[str]:
    for value in values:
        text = str(value or "").strip()
        if text.lower().startswith(("http://", "https://")):
            return text
    return None


def _infer_baseline_reference_audio_url(
    db: CentralDatabaseService,
    *,
    item: "EvaluationItem",
    artifact_lookup: Dict[str, List[AudioArtifactPayload]],
) -> Optional[str]:
    baseline_artifacts = artifact_lookup.get("baseline") or []
    for artifact in baseline_artifacts:
        direct = _first_http_url(artifact.public_url, artifact.storage_uri)
        if direct:
            return direct

    baseline_run_id = str(item.baseline_run_id or "").strip()
    if baseline_run_id:
        try:
            run_record = db.get_calibration_run(run_id=baseline_run_id)
            run_meta = run_record.metadata if run_record and isinstance(run_record.metadata, dict) else {}
            if isinstance(run_meta, dict):
                render_meta = run_meta.get("render") if isinstance(run_meta.get("render"), dict) else {}
                direct = _first_http_url(
                    run_meta.get("source_storage_uri"),
                    run_meta.get("source_uri"),
                    render_meta.get("source_storage_uri"),
                    render_meta.get("source_uri"),
                )
                if direct:
                    return direct
        except Exception:
            pass

    source_analysis_id = _infer_source_analysis_id(item=item, artifact_lookup=artifact_lookup)
    return _baseline_reference_audio_url_from_analysis_id(source_analysis_id)


def _serialize_run_bundle(db: CentralDatabaseService, run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    run_id_val = (run_id or "").strip()
    if not run_id_val:
        return None

    run = db.get_calibration_run(run_id=run_id_val)
    if not run:
        return None

    version = db.get_run_version(run_id=run_id_val)
    artifacts = db.get_audio_artifacts_for_run(run_id=run_id_val)
    return {
        "run": _model_dump(_serialize_run(run)),
        "run_version": _model_dump(_serialize_run_version(version)) if version else None,
        "artifacts": [_model_dump(_serialize_artifact(item)) for item in artifacts],
    }


def _iso_datetime(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    try:
        return value.isoformat()
    except Exception:
        return None


def _run_age_seconds(started_at: Optional[datetime]) -> Optional[int]:
    if not started_at:
        return None
    try:
        now = datetime.now(started_at.tzinfo) if started_at.tzinfo is not None else datetime.utcnow()
        age_seconds = int((now - started_at).total_seconds())
        return max(age_seconds, 0)
    except Exception:
        return None


def _collect_item_lane_progress(
    db: CentralDatabaseService,
    *,
    baseline_run_id: Optional[str],
    candidate_a_run_id: Optional[str],
    candidate_b_run_id: Optional[str],
) -> Dict[str, Any]:
    lane_specs: List[tuple[str, Optional[str]]] = [
        ("baseline", baseline_run_id),
        ("A", candidate_a_run_id),
        ("B", candidate_b_run_id),
    ]
    lanes: List[Dict[str, Any]] = []
    missing_lanes: List[str] = []
    all_ready = True

    for lane, run_id in lane_specs:
        run_id_val = (run_id or "").strip()
        lane_payload: Dict[str, Any] = {
            "lane": lane,
            "run_id": run_id_val or None,
            "ready": False,
            "artifact_count": 0,
            "artifact_types": [],
            "strict_reference_ok": True,
            "run_outcome": None,
            "run_started_at": None,
            "run_completed_at": None,
            "run_age_seconds": None,
            "stalled_in_queue": False,
        }

        if not run_id_val:
            if lane == "baseline":
                lane_payload["ready"] = True
                lane_payload["not_required"] = True
                lane_payload["strict_reference_ok"] = False
                lane_payload["reason"] = "No cloud-readable baseline reference artifact is available; A/B review can continue."
                lanes.append(lane_payload)
                continue
            lane_payload["strict_reference_ok"] = True
            lanes.append(lane_payload)
            missing_lanes.append(lane)
            all_ready = False
            continue

        try:
            artifacts = db.get_audio_artifacts_for_run(run_id=run_id_val)
        except Exception:
            artifacts = []

        run_record: Optional[CalibrationRun] = None
        try:
            run_record = db.get_calibration_run(run_id=run_id_val)
        except Exception:
            run_record = None

        if run_record:
            lane_payload["run_outcome"] = str(run_record.outcome or "").strip() or None
            lane_payload["run_started_at"] = _iso_datetime(run_record.started_at)
            lane_payload["run_completed_at"] = _iso_datetime(run_record.completed_at)
            lane_payload["run_age_seconds"] = _run_age_seconds(run_record.started_at)

        serialized = [_serialize_artifact(item) for item in artifacts]
        artifact_types = [str(item.artifact_type or "").strip() for item in serialized if str(item.artifact_type or "").strip()]

        lane_payload["artifact_count"] = len(serialized)
        lane_payload["artifact_types"] = artifact_types
        lane_payload["ready"] = len(serialized) > 0

        if lane == "baseline":
            strict_reference_ok = any(
                str(item.artifact_type or "").strip() == "reference_song"
                and str((item.render_recipe or {}).get("source_type") or "").strip() == "assimilated_song"
                for item in serialized
            )
            lane_payload["strict_reference_ok"] = strict_reference_ok
            lane_payload["ready"] = lane_payload["ready"] and strict_reference_ok

        if not lane_payload["ready"]:
            run_outcome = str(lane_payload.get("run_outcome") or "").strip().lower()
            run_age_seconds = lane_payload.get("run_age_seconds")
            if isinstance(run_age_seconds, int) and run_outcome == "queued" and run_age_seconds >= _QUEUE_STALL_HINT_SECONDS:
                lane_payload["stalled_in_queue"] = True
                lane_payload["reason"] = (
                    f"Run has remained queued for {run_age_seconds}s with no artifacts; render worker may be stalled."
                )
            elif run_outcome == "failure":
                lane_payload["reason"] = "Run is marked as failure and no artifacts are available."
            elif run_outcome == "queued" and isinstance(run_age_seconds, int):
                lane_payload["reason"] = f"Run is queued ({run_age_seconds}s) and artifacts are not ready yet."
            missing_lanes.append(lane)
            all_ready = False

        lanes.append(lane_payload)

    return {
        "all_ready": all_ready,
        "missing_lanes": missing_lanes,
        "lanes": lanes,
    }


def _safe_json_load(value: Any, default: Any) -> Any:
    try:
        if isinstance(value, str):
            parsed = json.loads(value)
            return parsed
    except Exception:
        return default
    return value if value is not None else default


def _fetch_pairwise_judgments(db: CentralDatabaseService, *, item_id: str) -> List[Dict[str, Any]]:
    engine = _require_postgres_engine(db)
    with engine.connect() as conn_pg:
        rows = conn_pg.execute(
            text(
                """
                SELECT judgment_id, item_id, preferred_candidate, closer_to_target,
                       better_feel, more_musical, confidence, created_at
                FROM public.pairwise_judgments
                WHERE item_id = :item_id
                ORDER BY created_at ASC
                """
            ),
            {"item_id": item_id},
        ).mappings().all()
    return [
        {
            "judgment_id": row["judgment_id"],
            "item_id": row["item_id"],
            "preferred_candidate": row["preferred_candidate"],
            "closer_to_target": row["closer_to_target"],
            "better_feel": row["better_feel"],
            "more_musical": row["more_musical"],
            "confidence": row["confidence"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _fetch_attribute_ratings(db: CentralDatabaseService, *, item_id: str) -> List[Dict[str, Any]]:
    engine = _require_postgres_engine(db)
    with engine.connect() as conn_pg:
        rows = conn_pg.execute(
            text(
                """
                SELECT rating_id, item_id, candidate_label,
                       stylistic_authenticity, groove_feel, dynamics, phrasing,
                       kit_balance, fill_behavior, human_realism, overall_usefulness,
                       created_at
                FROM public.attribute_ratings
                WHERE item_id = :item_id
                ORDER BY created_at ASC
                """
            ),
            {"item_id": item_id},
        ).mappings().all()
    return [
        {
            "rating_id": row["rating_id"],
            "item_id": row["item_id"],
            "candidate_label": row["candidate_label"],
            "stylistic_authenticity": row["stylistic_authenticity"],
            "groove_feel": row["groove_feel"],
            "dynamics": row["dynamics"],
            "phrasing": row["phrasing"],
            "kit_balance": row["kit_balance"],
            "fill_behavior": row["fill_behavior"],
            "human_realism": row["human_realism"],
            "overall_usefulness": row["overall_usefulness"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _fetch_item_feedback(db: CentralDatabaseService, *, item_id: str, drummer_slug: str) -> List[Dict[str, Any]]:
    engine = _require_postgres_engine(db)
    like_token = f'%"item_id": "{item_id}"%'
    with engine.connect() as conn_pg:
        rows = conn_pg.execute(
            text(
                """
                SELECT feedback_id, drummer_slug, rating, comment, author, submitted_at, metadata_json
                FROM public.calibration_feedback
                WHERE drummer_slug = :drummer_slug AND metadata_json::text LIKE :like_token
                ORDER BY submitted_at ASC
                """
            ),
            {"drummer_slug": drummer_slug, "like_token": like_token},
        ).mappings().all()
    output: List[Dict[str, Any]] = []
    for row in rows:
        output.append(
            {
                "feedback_id": row["feedback_id"],
                "drummer_slug": row["drummer_slug"],
                "rating": row["rating"],
                "comment": row["comment"],
                "author": row["author"],
                "submitted_at": row["submitted_at"],
                "metadata": _safe_json_load(row["metadata_json"], {}),
            }
        )
    return output


@router.get("/drummers", response_model=List[DrummerListItem])
async def list_drummers(db: CentralDatabaseService = Depends(get_db_service)) -> List[DrummerListItem]:
    async def _db_call_with_timeout(func, *args, timeout: float = 6.0, default=None, **kwargs):
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)
        except Exception:
            return default

    try:
        rows = await _db_call_with_timeout(db.get_drummers, timeout=8.0, default=[]) or []
        semaphore = asyncio.Semaphore(4)

        async def _build_drummer_item(row: Dict[str, Any]) -> Optional[DrummerListItem]:
            try:
                async with semaphore:
                    slug = _slug_from_row(row)
                    if not slug:
                        return None
                    display_name = _display_name_from_row(row, slug)

                    completion_info: Optional[CompletionStatusInfo] = None
                    within = row.get("metrics_within") or row.get("metrics_within_tolerance")
                    total = row.get("metrics_compared") or row.get("metrics_total")
                    if within is not None or total is not None:
                        try:
                            completion_info = _completion_from_counts(
                                int(within) if within is not None else None,
                                int(total) if total is not None else None,
                            )
                        except Exception:
                            completion_info = None

                    latest_run: Optional["CalibrationRun"] = await _db_call_with_timeout(
                        db.get_latest_calibration_run,
                        drummer_slug=slug,
                        timeout=4.0,
                        default=None,
                    )

                    if completion_info is None:
                        completion_info = _completion_from_run(latest_run)

                    latest_run_at = None
                    metrics_within_int: Optional[int] = None
                    metrics_total_int: Optional[int] = None
                    if latest_run:
                        latest_run_at = latest_run.completed_at or latest_run.started_at
                        metrics_within_int = latest_run.within_tolerance_count
                        metrics_total_int = latest_run.total_compared

                    assimilation_status = {
                        "status": "unknown",
                        "ready_for_calibration": False,
                        "missing_steps": ["status_check_deferred"],
                    }

                    return DrummerListItem(
                        slug=slug,
                        displayName=display_name,
                        completionStatus=completion_info,
                        assimilationStatus=assimilation_status,
                        latestRunAt=latest_run_at,
                        metricsWithin=metrics_within_int,
                        metricsCompared=metrics_total_int,
                    )
            except Exception as row_exc:
                logger.warning(f"Skipping drummer row due to roster serialization error: {row_exc}")
                return None

        built_items = await asyncio.gather(*[_build_drummer_item(row) for row in rows])
        results: List[DrummerListItem] = [item for item in built_items if item is not None]
        return sorted(results, key=lambda item: item.displayName.lower())
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/analysis/{analysis_id}/reference-audio")
async def get_analysis_reference_audio(analysis_id: str):
    analysis_id = (analysis_id or "").strip()
    if not analysis_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing analysis id")

    db = get_db_service()
    engine = _require_postgres_engine(db)

    try:
        with engine.connect() as conn_pg:
            stem_rows = conn_pg.execute(
                text(
                    """
                    SELECT stem_name, file_path
                    FROM public.stem_artifacts
                    WHERE analysis_id = :analysis_id
                    """
                ),
                {"analysis_id": analysis_id},
            ).mappings().all()

            artifact_rows = conn_pg.execute(
                text(
                    """
                    SELECT artifact_role, file_path
                    FROM public.analysis_artifacts
                    WHERE analysis_id = :analysis_id
                    ORDER BY created_at DESC
                    """
                ),
                {"analysis_id": analysis_id},
            ).mappings().all()

            spa_row = conn_pg.execute(
                text(
                    """
                    SELECT source_file
                    FROM public.song_performance_analysis
                    WHERE analysis_id = :analysis_id
                    LIMIT 1
                    """
                ),
                {"analysis_id": analysis_id},
            ).mappings().first()

        candidates: List[str] = []

        preferred_stems = [
            row.get("file_path")
            for row in stem_rows
            if str(row.get("stem_name") or "").strip().lower() in {"drums", "drum"} and str(row.get("file_path") or "").strip()
        ]
        fallback_stems = [
            row.get("file_path")
            for row in stem_rows
            if str(row.get("file_path") or "").strip()
        ]
        preferred_artifacts = [
            row.get("file_path")
            for row in artifact_rows
            if str(row.get("artifact_role") or "").strip().lower() in {"drums", "drum", "mix", "preview"}
            and str(row.get("file_path") or "").strip()
        ]
        fallback_artifacts = [
            row.get("file_path")
            for row in artifact_rows
            if str(row.get("file_path") or "").strip()
        ]

        for value in preferred_stems + fallback_stems + preferred_artifacts + fallback_artifacts:
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())

        source_file = str((spa_row or {}).get("source_file") or "").strip()
        if source_file:
            candidates.append(source_file)

        seen: set[str] = set()
        deduped_candidates: List[str] = []
        for value in candidates:
            if value in seen:
                continue
            seen.add(value)
            deduped_candidates.append(value)

        for candidate in deduped_candidates:
            if candidate.lower().startswith(("http://", "https://")):
                return RedirectResponse(url=candidate, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

            local_path = _resolve_local_path(candidate)
            if local_path and local_path.is_file():
                return FileResponse(path=str(local_path), filename=local_path.name)

        try:
            with engine.connect() as conn_pg:
                artifact_rows = conn_pg.execute(
                    text(
                        """
                        SELECT aa.run_id, aa.artifact_id, aa.artifact_type
                        FROM public.audio_artifacts aa
                        LEFT JOIN public.calibration_runs cr ON cr.run_id = aa.run_id
                        WHERE COALESCE(cr.metadata_json::text, '') ILIKE :analysis_token
                           OR COALESCE(aa.render_recipe_json::text, '') ILIKE :analysis_token
                        LIMIT 100
                        """
                    ),
                    {"analysis_token": f"%{analysis_id}%"},
                ).mappings().all()

            preferred_types = {"mix", "preview", "audio", "reference", "reference_song"}
            preferred_rows = [
                row
                for row in artifact_rows
                if str(row.get("artifact_type") or "").strip().lower() in preferred_types
            ]
            for row in (preferred_rows or artifact_rows):
                run_id_val = str(row.get("run_id") or "").strip()
                artifact_id_val = str(row.get("artifact_id") or "").strip()
                if not run_id_val or not artifact_id_val:
                    continue
                return RedirectResponse(
                    url=f"/calibration/audio-artifacts/{quote(run_id_val)}/{quote(artifact_id_val)}",
                    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                )
        except Exception:
            logger.warning("analysis_reference_audio_artifact_fallback_failed analysis_id=%s", analysis_id, exc_info=True)

        try:
            with engine.connect() as conn_pg:
                analysis_row = conn_pg.execute(
                    text(
                        """
                        SELECT
                            CAST(spa.drummer_id AS TEXT) AS drummer_id,
                            CAST(COALESCE(d.drummer_id, '') AS TEXT) AS drummer_slug,
                            COALESCE(d.display_name, d.name, '') AS drummer_name
                        FROM public.song_performance_analysis spa
                        LEFT JOIN public.drummers d ON CAST(d.id AS TEXT) = CAST(spa.drummer_id AS TEXT)
                        WHERE spa.analysis_id = :analysis_id
                        LIMIT 1
                        """
                    ),
                    {"analysis_id": analysis_id},
                ).mappings().first()

            drummer_candidates: List[str] = []
            if analysis_row:
                for key in ("drummer_slug", "drummer_id", "drummer_name"):
                    value = str((analysis_row or {}).get(key) or "").strip()
                    if value:
                        drummer_candidates.append(value)

            seen_slugs: set[str] = set()
            for drummer_slug in drummer_candidates:
                normalized_slug = drummer_slug.strip()
                if not normalized_slug or normalized_slug in seen_slugs:
                    continue
                seen_slugs.add(normalized_slug)

                runs = db.get_calibration_runs(
                    drummer_slug=normalized_slug,
                    limit=40,
                    include_metrics=True,
                )

                def _run_matches_analysis(run_obj: Any) -> bool:
                    meta = run_obj.metadata if isinstance(getattr(run_obj, "metadata", None), dict) else {}
                    try:
                        payload = json.dumps(meta, sort_keys=True)
                    except Exception:
                        payload = str(meta)
                    return analysis_id in payload

                prioritized_runs = [run for run in runs if _run_matches_analysis(run)]
                for run in (prioritized_runs or runs):
                    run_id_val = str(getattr(run, "run_id", "") or "").strip()
                    if not run_id_val:
                        continue
                    artifact_id = _pick_reference_artifact_id(db, baseline_run_id=run_id_val)
                    if artifact_id:
                        return RedirectResponse(
                            url=f"/calibration/audio-artifacts/{quote(run_id_val)}/{quote(artifact_id)}",
                            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                        )
        except Exception:
            logger.warning("analysis_reference_audio_drummer_fallback_failed analysis_id=%s", analysis_id, exc_info=True)

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No playable reference audio found for analysis")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/audio-artifacts/{run_id}/{artifact_id}")
async def stream_audio_artifact(
    run_id: str,
    artifact_id: str,
    db: CentralDatabaseService = Depends(get_db_service),
):
    run_id_val = (run_id or "").strip()
    artifact_id_val = (artifact_id or "").strip()
    if not run_id_val or not artifact_id_val:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing run or artifact id")

    try:
        artifacts = db.get_audio_artifacts_for_run(run_id=run_id_val)
        selected = next(
            (item for item in artifacts if str(getattr(item, "artifact_id", "") or "").strip() == artifact_id_val),
            None,
        )
        if not selected:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio artifact not found")

        source_url = _artifact_url_service.build_url(str(getattr(selected, "storage_uri", "") or ""))
        direct_http = str(source_url or "").strip()
        if direct_http.lower().startswith(("http://", "https://")):
            return RedirectResponse(url=direct_http, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

        local_path = _resolve_local_path(direct_http) if direct_http else None
        if not local_path:
            local_path = _resolve_local_path(getattr(selected, "storage_uri", ""))
        if local_path and local_path.is_file():
            return FileResponse(path=str(local_path), filename=local_path.name)

        repaired_path = _repair_legacy_preview_artifact_if_missing(
            db,
            run_id=run_id_val,
            artifact=selected,
        )
        if repaired_path and repaired_path.is_file():
            return FileResponse(path=str(repaired_path), filename=repaired_path.name)

        if direct_http.startswith("/"):
            return RedirectResponse(url=direct_http, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playable artifact file not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/evaluation-items/{item_id}/progress")
async def get_evaluation_item_progress(item_id: str) -> Dict[str, Any]:
    item_id = (item_id or "").strip()
    if not item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing item id")

    try:
        db = await asyncio.wait_for(
            asyncio.to_thread(get_db_service),
            timeout=5.0,
        )
    except TimeoutError:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Database service initialization timed out")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    try:
        item = await asyncio.wait_for(
            asyncio.to_thread(db.get_evaluation_item, item_id=item_id),
            timeout=8.0,
        )
    except TimeoutError:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Evaluation item lookup timed out")
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation item not found")

    try:
        progress = await asyncio.wait_for(
            asyncio.to_thread(
                _collect_item_lane_progress,
                db,
                baseline_run_id=item.baseline_run_id,
                candidate_a_run_id=item.candidate_a_run_id,
                candidate_b_run_id=item.candidate_b_run_id,
            ),
            timeout=8.0,
        )
        degraded = False
    except TimeoutError:
        logger.warning("Evaluation item progress lookup timed out item_id=%s; returning degraded lane payload", item_id)
        progress = {
            "all_ready": False,
            "missing_lanes": ["A", "B"],
            "lanes": [
                {
                    "lane": "baseline",
                    "run_id": (item.baseline_run_id or "").strip() or None,
                    "ready": not bool((item.baseline_run_id or "").strip()),
                    "artifact_count": 0,
                    "artifact_types": [],
                    "strict_reference_ok": False,
                    "reason": "Baseline progress lookup timed out; retrying.",
                },
                {
                    "lane": "A",
                    "run_id": (item.candidate_a_run_id or "").strip() or None,
                    "ready": False,
                    "artifact_count": 0,
                    "artifact_types": [],
                    "strict_reference_ok": True,
                    "reason": "Progress lookup timed out; retrying.",
                },
                {
                    "lane": "B",
                    "run_id": (item.candidate_b_run_id or "").strip() or None,
                    "ready": False,
                    "artifact_count": 0,
                    "artifact_types": [],
                    "strict_reference_ok": True,
                    "reason": "Progress lookup timed out; retrying.",
                },
            ],
        }
        degraded = True
    except Exception as exc:
        logger.warning("Evaluation item progress failed item_id=%s; returning degraded lane payload err=%s", item_id, exc)
        progress = {
            "all_ready": False,
            "missing_lanes": ["A", "B"],
            "lanes": [
                {
                    "lane": "baseline",
                    "run_id": (item.baseline_run_id or "").strip() or None,
                    "ready": not bool((item.baseline_run_id or "").strip()),
                    "artifact_count": 0,
                    "artifact_types": [],
                    "strict_reference_ok": False,
                    "reason": "Baseline progress lookup failed; retrying.",
                },
                {
                    "lane": "A",
                    "run_id": (item.candidate_a_run_id or "").strip() or None,
                    "ready": False,
                    "artifact_count": 0,
                    "artifact_types": [],
                    "strict_reference_ok": True,
                    "reason": "Progress lookup failed; retrying.",
                },
                {
                    "lane": "B",
                    "run_id": (item.candidate_b_run_id or "").strip() or None,
                    "ready": False,
                    "artifact_count": 0,
                    "artifact_types": [],
                    "strict_reference_ok": True,
                    "reason": "Progress lookup failed; retrying.",
                },
            ],
        }
        degraded = True

    return {
        "item_id": item.item_id,
        "all_ready": bool(progress.get("all_ready")),
        "missing_lanes": progress.get("missing_lanes") or [],
        "lanes": progress.get("lanes") or [],
        "queue_stall_hint_seconds": _QUEUE_STALL_HINT_SECONDS,
        "degraded": degraded,
    }
# ASGI application factory
app = FastAPI(title="DrumTrackAI Calibration API")

_allowed_origins = [
    "https://drumtrackai.netlify.app",
    "https://www.drumtrackai.netlify.app",
    "https://drumtrackai.net",
    "https://www.drumtrackai.net",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

_allowed_origins = sorted(set(_allowed_origins + _csv_env_values("CALIBRATION_CORS_ORIGINS")))
_allowed_origin_regex = os.getenv(
    "CALIBRATION_CORS_ORIGIN_REGEX",
    r"https://(?:[a-z0-9-]+\.)*(?:netlify\.app|drumtrackai\.net)|http://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=_allowed_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,
)


@app.on_event("startup")
async def _warm_database_on_startup() -> None:
    # Fail fast on deployment/config drift instead of letting the first browser
    # request discover an uninitialized or non-Postgres DB service.
    await asyncio.to_thread(get_db_service)


@app.on_event("startup")
async def _auto_populate_on_startup() -> None:
    if not _parse_env_bool("ASSIMILATION_AUTO_POPULATE_ON_STARTUP", default=False):
        return

    base_dir = (
        str(os.getenv("ASSIMILATION_AUTO_POPULATE_BASE_DIR", "")).strip()
        or str(os.getenv("PROCESSED_STEMS_BASE_DIR", "")).strip()
    )
    if not base_dir:
        logger.warning("ASSIMILATION_AUTO_POPULATE_ON_STARTUP enabled but no base dir configured")
        return

    drummers_raw = str(os.getenv("ASSIMILATION_AUTO_POPULATE_DRUMMERS", "")).strip()
    if drummers_raw:
        logger.info("ASSIMILATION_AUTO_POPULATE_DRUMMERS is set but ignored; auto-populate processes all discovered drummers")
    max_events_per_stem = int(str(os.getenv("ASSIMILATION_AUTO_MAX_EVENTS_PER_STEM", "5000")).strip() or "5000")
    compute_hashes = _parse_env_bool("ASSIMILATION_AUTO_COMPUTE_HASHES", default=False)
    hash_max_bytes = int(str(os.getenv("ASSIMILATION_AUTO_HASH_MAX_BYTES", "0")).strip() or "0")

    started = _start_auto_assimilation_population(
        base_dir=base_dir,
        max_events_per_stem=max_events_per_stem,
        compute_hashes=compute_hashes,
        hash_max_bytes=hash_max_bytes,
    )
    if started:
        logger.info("Started automatic assimilation population on startup")
    else:
        logger.info("Skipped startup auto-population; job already running")

# Static artifacts mount
_artifacts_root = (Path(__file__).resolve().parents[1] / "artifacts").resolve()
try:
    _artifacts_root.mkdir(parents=True, exist_ok=True)
    calib_dir = _artifacts_root / "calibration"
    calib_dir.mkdir(parents=True, exist_ok=True)

    app.mount(
        "/artifacts",
        StaticFiles(directory=str(_artifacts_root), html=False),
        name="artifacts",
    )
    app.mount(
        "/static/calibration_artifacts",
        StaticFiles(directory=str(calib_dir), html=False),
        name="calibration_static",
    )
except Exception:
    pass

# Routers are registered at end of file after all routes are defined.


@router.post("/storage/presign-upload", response_model=StoragePresignUploadResponse)
async def presign_upload(
    request: Request,
    payload: StoragePresignUploadRequest,
    db: CentralDatabaseService = Depends(get_db_service),
):
    user_id = _require_user(request)
    slug = (payload.drummer_slug or "").strip()
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drummer slug")
    if not db.user_has_access_to_drummer(user_id=user_id, drummer_id=slug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    bucket = os.getenv("AWS_S3_BUCKET", "").strip()
    region = os.getenv("AWS_REGION", "").strip() or os.getenv("AWS_DEFAULT_REGION", "").strip()
    if not bucket or not region:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="S3 not configured")

    try:
        import boto3  # type: ignore
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="boto3 not installed")

    file_name = (payload.file_name or "upload.bin").strip()
    safe_name = file_name.replace("\\", "/").split("/")[-1]
    safe_name = "".join(ch if ch.isalnum() or ch in ("_", ".", "-") else "_" for ch in safe_name)
    run_id = (payload.run_id or str(uuid.uuid4())).strip()
    key = f"drummers/{slug}/runs/{run_id}/audio/{uuid.uuid4()}_{safe_name}"

    s3 = boto3.client("s3", region_name=region)
    fields = {"Content-Type": (payload.content_type or "application/octet-stream").strip()}
    conditions = [["starts-with", "$Content-Type", ""]]
    post = s3.generate_presigned_post(
        Bucket=bucket,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=900,
    )

    db.log_calibration_audit(
        actor_user_id=user_id,
        drummer_id=slug,
        run_id=run_id,
        action="storage_presign_upload",
        payload={"key": key},
    )

    return StoragePresignUploadResponse(
        bucket=bucket,
        key=key,
        url=post.get("url", ""),
        fields=post.get("fields", {}),
        expires_in=900,
    )


@router.post("/storage/presign-download", response_model=StoragePresignDownloadResponse)
async def presign_download(
    request: Request,
    payload: StoragePresignDownloadRequest,
    db: CentralDatabaseService = Depends(get_db_service),
):
    user_id = _require_user(request)
    slug = (payload.drummer_slug or "").strip()
    key = (payload.key or "").strip()
    if not slug or not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing parameters")
    if not db.user_has_access_to_drummer(user_id=user_id, drummer_id=slug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    bucket = os.getenv("AWS_S3_BUCKET", "").strip()
    region = os.getenv("AWS_REGION", "").strip() or os.getenv("AWS_DEFAULT_REGION", "").strip()
    if not bucket or not region:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="S3 not configured")

    try:
        import boto3  # type: ignore
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="boto3 not installed")

    s3 = boto3.client("s3", region_name=region)
    try:
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=600,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    db.log_calibration_audit(
        actor_user_id=user_id,
        drummer_id=slug,
        run_id=None,
        action="storage_presign_download",
        payload={"key": key},
    )

    return StoragePresignDownloadResponse(bucket=bucket, key=key, url=url, expires_in=600)


@router.post("/jobs/analysis", response_model=Dict[str, Any])
async def enqueue_analysis_job(
    request: Request,
    payload: AnalysisJobCreateRequest,
    db: CentralDatabaseService = Depends(get_db_service),
):
    user_id = _require_user(request)
    slug = (payload.drummer_slug or "").strip()
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drummer slug")
    if not db.user_has_access_to_drummer(user_id=user_id, drummer_id=slug):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    job_id = db.create_analysis_job(drummer_id=slug, input_json=payload.input_json)
    if not job_id:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to create job")
    db.log_calibration_audit(
        actor_user_id=user_id,
        drummer_id=slug,
        run_id=None,
        action="analysis_job_enqueued",
        payload={"job_id": job_id, "input_json": payload.input_json},
    )
    return {"status": "queued", "job_id": job_id}


@router.get("/jobs/{job_id}", response_model=AnalysisJobResponse)
async def get_analysis_job(job_id: str, request: Request, db: CentralDatabaseService = Depends(get_db_service)) -> AnalysisJobResponse:
    user_id = _require_user(request)
    ref = db.get_analysis_job(job_id=job_id)
    if not ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    drummer_id = str(ref.get("drummer_id") or "").strip()
    if not drummer_id or not db.user_has_access_to_drummer(user_id=user_id, drummer_id=drummer_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return AnalysisJobResponse(**ref)


@router.get("/analysis/{analysis_id}", response_model=AnalysisDetailPayload)
async def get_analysis_detail(analysis_id: str, db: CentralDatabaseService = Depends(get_db_service)) -> AnalysisDetailPayload:
    analysis_id = (analysis_id or "").strip()
    if not analysis_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing analysis id")

    try:
        engine = _require_postgres_engine(db)
        with engine.connect() as conn_pg:
            row = conn_pg.execute(
                text(
                    """
                    SELECT spa.analysis_id,
                           spa.source_file,
                           spa.tempo_bpm,
                           spa.time_signature,
                           spa.duration_sec,
                           spa.created_at,
                           d.drummer_id AS drummer_slug,
                           s.title AS song_title
                    FROM public.song_performance_analysis spa
                    LEFT JOIN public.drummers d ON CAST(d.id AS TEXT) = CAST(spa.drummer_id AS TEXT)
                    LEFT JOIN public.songs s ON s.id = spa.song_id
                    WHERE spa.analysis_id = :analysis_id
                    LIMIT 1
                    """
                ),
                {"analysis_id": analysis_id},
            ).mappings().first()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found")

        hit_event_count: Optional[int] = None
        try:
            with engine.connect() as conn_pg:
                count_row = conn_pg.execute(
                    text("SELECT COUNT(1) FROM public.drum_hit_events WHERE analysis_id = :analysis_id"),
                    {"analysis_id": analysis_id},
                ).first()
            hit_event_count = int((count_row[0] if count_row else 0) or 0)
        except Exception:
            hit_event_count = None

        return AnalysisDetailPayload(
            analysis_id=str(row["analysis_id"]),
            drummer_slug=str(row["drummer_slug"]).strip() if row["drummer_slug"] is not None else None,
            song_title=str(row["song_title"]).strip() if row["song_title"] is not None else None,
            source_file=str(row["source_file"]).strip() if row["source_file"] is not None else None,
            tempo_bpm=float(row["tempo_bpm"]) if row["tempo_bpm"] is not None else None,
            time_signature=str(row["time_signature"]).strip() if row["time_signature"] is not None else None,
            duration_sec=float(row["duration_sec"]) if row["duration_sec"] is not None else None,
            created_at=str(row["created_at"]).strip() if row["created_at"] is not None else None,
            hit_event_count=hit_event_count,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

 
@router.post("/evaluation-items/{item_id}/judgment")
async def submit_pairwise_judgment(
    item_id: str,
    payload: PairwiseJudgmentSubmit,
    db: CentralDatabaseService = Depends(get_db_service),
) -> Dict[str, Any]:
    item_id = (item_id or "").strip()
    if not item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing item id")

    try:
        judgment_id = db.log_pairwise_judgment(
            item_id=item_id,
            preferred_candidate=payload.preferred_candidate,
            closer_to_target=payload.closer_to_target,
            better_feel=payload.better_feel,
            more_musical=payload.more_musical,
            confidence=payload.confidence,
        )
        if not judgment_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to store pairwise judgment")
        return {"status": "ok", "judgment_id": judgment_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/health", response_model=CalibrationHealthPayload)
async def calibration_health() -> CalibrationHealthPayload:
    build_marker = CALIBRATION_API_BUILD_MARKER
    db_path = None
    svc = CentralDatabaseService.get_instance()
    try:
        _ = svc.initialize()
        db_path = getattr(svc, "_db_path", None)
    except Exception:
        db_path = None

    calibration_tables = {
        "calibration_adjustments": False,
        "calibration_runs": False,
        "calibration_feedback": False,
        "evaluation_sessions": False,
        "evaluation_items": False,
        "pairwise_judgments": False,
        "attribute_ratings": False,
    }
    notes: List[str] = []
    notes.append(f"build_marker={build_marker}")
    notes.append(f"instance_id={CALIBRATION_API_INSTANCE_ID}")
    for key in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        value = str(os.getenv(key, "")).strip()
        if value:
            notes.append(f"{key}={value}")
    try:
        engine_active = bool(getattr(svc, "_engine", None) is not None)
        backend_env = str(os.getenv("DB_BACKEND", "")).strip().lower()
        db_url_env = str(os.getenv("DATABASE_URL", "")).strip()
        url_scheme = db_url_env.split("://", 1)[0] if "://" in db_url_env else ""
        notes.append(f"engine_active={engine_active}")
        if backend_env:
            notes.append(f"db_backend={backend_env}")
        if url_scheme:
            notes.append(f"db_url_scheme={url_scheme}")
    except Exception:
        pass
    try:
        if engine_active:
            for table_name in calibration_tables.keys():
                cols = svc._table_columns(table_name)
                calibration_tables[table_name] = bool(cols)
    except Exception as exc:
        notes.append(f"schema_probe_failed: {exc}")

    missing_tables = [name for name, ok in calibration_tables.items() if not ok]
    if missing_tables:
        notes.append("missing_tables=" + ",".join(missing_tables))

    db_exists = False
    try:
        is_pg = engine_active and ((backend_env in {"postgres", "postgresql"}) or url_scheme.startswith("postgres"))
    except Exception:
        is_pg = False
    if is_pg:
        db_exists = True
    elif db_path:
        try:
            db_exists = bool(Path(str(db_path)).exists())
        except Exception:
            db_exists = False

    masked_db_path = None
    if db_path:
        try:
            s = str(db_path)
            i = s.find('://')
            if i != -1:
                j = s.find('@', i + 3)
                if j != -1:
                    userpass = s[i + 3 : j]
                    user = userpass.split(':', 1)[0] if ':' in userpass else userpass
                    s = s[: i + 3] + user + ':***' + s[j:]
            masked_db_path = s
        except Exception:
            masked_db_path = str(db_path)

    status_text = "ok" if db_exists and not missing_tables else "degraded"
    return CalibrationHealthPayload(
        status=status_text,
        db_path=masked_db_path,
        db_exists=db_exists,
        calibration_tables=calibration_tables,
        notes=notes,
    )


@router.get("/db-diagnostics")
async def db_diagnostics() -> Dict[str, Any]:
    backend_env = str(os.getenv("DB_BACKEND", "")).strip().lower()
    db_url_env = str(os.getenv("DATABASE_URL", "")).strip()
    out: Dict[str, Any] = {
        "db_backend": backend_env or None,
        "has_database_url": bool(db_url_env),
        "runtime": _runtime_diagnostics(),
    }
    configured = (backend_env in {"postgres", "postgresql"}) or db_url_env.lower().startswith("postgres")
    out["configured"] = configured
    if not configured:
        return out
    try:
        engine = create_engine(db_url_env, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                row = conn.execute(text("SELECT current_user, current_database()"))
                info = row.first()
                if info is not None:
                    out["current_user"] = info[0]
                    out["current_database"] = info[1]
            except Exception:
                pass
        out["connect_ok"] = True
    except Exception as e:
        out["connect_ok"] = False
        out["error"] = str(e)
    return out


@router.get("/assimilation/auto-populate-status")
async def auto_populate_status() -> Dict[str, Any]:
    with _AUTO_ASSIMILATION_LOCK:
        return {
            "running": bool(_AUTO_ASSIMILATION_STATE.get("running")),
            "last_started_at": _AUTO_ASSIMILATION_STATE.get("last_started_at"),
            "last_completed_at": _AUTO_ASSIMILATION_STATE.get("last_completed_at"),
            "last_error": _AUTO_ASSIMILATION_STATE.get("last_error"),
            "last_summary": _AUTO_ASSIMILATION_STATE.get("last_summary"),
        }


@router.post("/assimilation/auto-populate")
async def trigger_auto_populate(payload: AutoAssimilationPopulateRequest) -> Dict[str, Any]:
    base_dir = (payload.base_dir or "").strip()
    if not base_dir:
        base_dir = (
            str(os.getenv("ASSIMILATION_AUTO_POPULATE_BASE_DIR", "")).strip()
            or str(os.getenv("PROCESSED_STEMS_BASE_DIR", "")).strip()
        )
    if not base_dir:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing base_dir")

    base_path = Path(base_dir).expanduser().resolve()
    if not base_path.exists() or not base_path.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid base_dir: {base_path}")

    started = _start_auto_assimilation_population(
        base_dir=str(base_path),
        max_events_per_stem=payload.max_events_per_stem,
        compute_hashes=payload.compute_hashes,
        hash_max_bytes=payload.hash_max_bytes,
    )
    if not started:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Auto-population job already running")

    return {
        "status": "queued",
        "base_dir": str(base_path),
        "scope": "all_discovered_drummers",
    }


@router.get("/training-export", response_model=CalibrationTrainingExportPayload)
async def export_training_dataset(
    drummer_slug: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=5000),
    db: CentralDatabaseService = Depends(get_db_service),
) -> CalibrationTrainingExportPayload:
    slug_filter = (drummer_slug or "").strip()
    items: List[Dict[str, Any]] = []

    try:
        engine = _require_postgres_engine(db)
        with engine.connect() as conn_pg:
            rows = conn_pg.execute(
                text(
                    """
                    SELECT *
                    FROM public.evaluation_items
                    WHERE (:slug_filter = '' OR target_drummer_slug = :slug_filter)
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {"slug_filter": slug_filter, "limit": int(limit)},
            ).mappings().all()

        for row in rows:
            item = db._row_to_evaluation_item(row)
            if not item:
                continue

            item_payload: Dict[str, Any] = {
                "item": {
                    "item_id": item.item_id,
                    "session_id": item.session_id,
                    "target_drummer_slug": item.target_drummer_slug,
                    "base_groove_id": item.base_groove_id,
                    "reference_artifact_id": item.reference_artifact_id,
                    "baseline_run_id": item.baseline_run_id,
                    "candidate_a_run_id": item.candidate_a_run_id,
                    "candidate_b_run_id": item.candidate_b_run_id,
                    "eval_mode": item.eval_mode,
                    "ab_mapping": item.ab_mapping,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                },
                "assimilation_status": _assimilation_status_for_slug(db, item.target_drummer_slug),
                "pairwise_judgments": _fetch_pairwise_judgments(db, item_id=item.item_id),
                "attribute_ratings": _fetch_attribute_ratings(db, item_id=item.item_id),
                "feedback": _fetch_item_feedback(
                    db,
                    item_id=item.item_id,
                    drummer_slug=item.target_drummer_slug,
                ),
                "runs": {
                    "baseline": _serialize_run_bundle(db, item.baseline_run_id),
                    "A": _serialize_run_bundle(db, item.candidate_a_run_id),
                    "B": _serialize_run_bundle(db, item.candidate_b_run_id),
                },
            }
            items.append(item_payload)

        return CalibrationTrainingExportPayload(
            exported_at=datetime.utcnow(),
            item_count=len(items),
            filters={"drummer_slug": slug_filter or None, "limit": int(limit)},
            items=items,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/evaluation-items/{item_id}/ratings")
async def submit_attribute_ratings(
    item_id: str,
    payload: AttributeRatingsSubmit,
    db: CentralDatabaseService = Depends(get_db_service),
) -> Dict[str, Any]:
    item_id = (item_id or "").strip()
    if not item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing item id")

    try:
        rating_id = db.log_attribute_rating(item_id=item_id, **_model_dump(payload))
        if not rating_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to store ratings")
        return {"status": "ok", "rating_id": rating_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

@router.get("/evaluation-items/{item_id}", response_model=EvaluationItemPayload)
async def get_evaluation_item(item_id: str) -> EvaluationItemPayload:
    item_id = (item_id or "").strip()
    if not item_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing item id")

    try:
        db = await asyncio.wait_for(
            asyncio.to_thread(get_db_service),
            timeout=5.0,
        )
        item = await asyncio.wait_for(
            asyncio.to_thread(db.get_evaluation_item, item_id=item_id),
            timeout=8.0,
        )
        if not item:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evaluation item not found")

        artifact_map: Dict[str, List[AudioArtifactPayload]] = {}
        for label, run_id in (
            ("baseline", item.baseline_run_id),
            ("A", item.candidate_a_run_id),
            ("B", item.candidate_b_run_id),
        ):
            run_id_val = (run_id or "").strip() if run_id else ""
            if not run_id_val:
                continue
            try:
                artifacts = await asyncio.wait_for(
                    asyncio.to_thread(db.get_audio_artifacts_for_run, run_id=run_id_val),
                    timeout=6.0,
                )
            except Exception:
                logger.warning("evaluation_item_artifacts_lookup_failed item_id=%s run_id=%s", item_id, run_id_val)
                artifacts = []
            artifact_map[label] = [_serialize_artifact(artifact) for artifact in artifacts]

        baseline_label = _infer_baseline_label(item=item, artifact_lookup=artifact_map)
        baseline_reference_audio_url = _infer_baseline_reference_audio_url(
            db,
            item=item,
            artifact_lookup=artifact_map,
        )
        return _serialize_item(
            item,
            artifact_map,
            baseline_label=baseline_label,
            baseline_reference_audio_url=baseline_reference_audio_url,
        )
    except TimeoutError:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Evaluation item lookup timed out")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

@router.get("/sessions/next", response_model=Optional[EvaluationSessionPayload])
async def get_next_session(
    reviewer_id: Optional[str] = Query(default=None),
    target_drummer_slug: Optional[str] = Query(default=None),
    db: CentralDatabaseService = Depends(get_db_service),
) -> Optional[EvaluationSessionPayload]:
    try:
        session = db.get_next_evaluation_session(
            reviewer_id=(reviewer_id or None),
            target_drummer_slug=(target_drummer_slug or None),
        )
        if not session:
            return None
        return _serialize_session(session)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: str, db: CentralDatabaseService = Depends(get_db_service)) -> Dict[str, Any]:
    session_id = (session_id or "").strip()
    if not session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing session id")

    try:
        ok = db.start_evaluation_session(session_id=session_id)
        if not ok:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        return {"status": "ok", "session_id": session_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: CentralDatabaseService = Depends(get_db_service)) -> Dict[str, Any]:
    run_id = (run_id or "").strip()
    if not run_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing run id")

    try:
        run = db.get_calibration_run(run_id=run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

        version = db.get_run_version(run_id=run_id)
        artifacts = db.get_audio_artifacts_for_run(run_id=run_id)

        payload: Dict[str, Any] = {
            "run": _model_dump(_serialize_run(run)),
            "artifacts": [_model_dump(_serialize_artifact(item)) for item in artifacts],
        }
        serialized_version = _serialize_run_version(version)
        if serialized_version:
            payload["run_version"] = _model_dump(serialized_version)

        return payload
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.get("/runs/{run_id}/artifacts", response_model=List[AudioArtifactPayload])
async def list_run_artifacts(run_id: str, db: CentralDatabaseService = Depends(get_db_service)) -> List[AudioArtifactPayload]:
    run_id = (run_id or "").strip()
    if not run_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing run id")

    try:
        artifacts = db.get_audio_artifacts_for_run(run_id=run_id)
        return [_serialize_artifact(item) for item in artifacts]
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/generate-candidates")
async def generate_candidates(
    payload: GenerateCandidatesRequest,
    request: Request,
    db: CentralDatabaseService = Depends(get_db_service),
) -> Dict[str, Any]:
    base_groove_id = (payload.base_groove_id or "").strip()
    target_slug = (payload.target_drummer_slug or "").strip()
    runtime = _runtime_diagnostics()
    if not base_groove_id or not target_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "stage": "request_validate",
                "message": "Missing base groove or drummer",
                "runtime": runtime,
            },
        )

    try:
        stage = "assimilation_status"

        logger.info(
            "generate_candidates_request build=%s instance=%s origin=%s host=%s drummer=%s base_groove=%s strict=%s include_baseline=%s",
            runtime.get("api_build_marker"),
            runtime.get("api_instance_id"),
            request.headers.get("origin"),
            request.headers.get("host"),
            target_slug,
            base_groove_id,
            bool(payload.strict_reference_baseline),
            bool(payload.include_baseline),
        )

        def _raise_stage_error(message: str, *, status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR) -> None:
            raise HTTPException(
                status_code=status_code,
                detail={
                    "stage": stage,
                    "message": message,
                    "runtime": runtime,
                },
            )

        # Strict gating: require full assimilation readiness before any generation.
        assimilation = _assimilation_status_for_slug(db, target_slug)
        if not assimilation.get("ready_for_calibration"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "stage": stage,
                    "message": "Assimilation not ready for calibration",
                    "assimilationStatus": assimilation,
                    "runtime": runtime,
                },
            )

        stage = "render_service_init"
        render_service = CalibrationRenderService(db)
        session_id: Optional[str] = None
        reviewer_id = (payload.reviewer_id or "").strip()
        if reviewer_id:
            stage = "reviewer_profile_upsert"
            reviewer_ok = db.upsert_reviewer_profile(reviewer_id=reviewer_id, display_name=reviewer_id)
            if not reviewer_ok:
                _raise_stage_error("Failed to upsert reviewer profile")

            stage = "evaluation_session_create"
            session_id = db.create_evaluation_session(
                reviewer_id=reviewer_id,
                target_drummer_slug=target_slug,
                app_version=f"calibration_phase2:{CALIBRATION_API_BUILD_MARKER}",
            )
            if not session_id:
                _raise_stage_error("Failed to create evaluation session")

        created_run_ids: List[str] = []
        generation_controls = payload.generation_controls if isinstance(payload.generation_controls, dict) else {}
        effective_base_groove_id = base_groove_id
        item_base_groove_id = base_groove_id
        baseline_analysis_id: Optional[str] = None
        baseline_run_id: Optional[str] = None
        reference_artifact_id: Optional[str] = None
        baseline_missing_reason: Optional[str] = None

        if payload.candidate_count < 2:
            _raise_stage_error("candidate_count must be at least 2 to produce A/B outputs", status_code=status.HTTP_400_BAD_REQUEST)

        if payload.include_baseline:
            stage = "baseline_source_select"
            baseline_source = _select_assimilation_baseline_source(
                db,
                drummer_slug=target_slug,
                require_cloud_uri=False,
            )
            if payload.strict_reference_baseline and not baseline_source:
                baseline_missing_reason = "No assimilated baseline source clip is available for this drummer. Proceeding with non-strict A/B queueing."
                logger.warning(
                    "strict_baseline_downgraded drummer=%s reason=%s",
                    target_slug,
                    baseline_missing_reason,
                )
            if baseline_source:
                source_groove_path = str(baseline_source.get("base_groove_path") or "").strip()
                if source_groove_path:
                    effective_base_groove_id = source_groove_path
                analysis_id = str(baseline_source.get("analysis_id") or "").strip()
                if analysis_id:
                    baseline_analysis_id = analysis_id
                    item_base_groove_id = f"assimilation:{analysis_id}"

                stage = "baseline_reference_run_create"
                baseline_ref: Optional[Dict[str, Any]] = None
                baseline_source_debug = _baseline_source_summary(baseline_source)
                baseline_storage_uri = _reference_storage_uri_from_baseline_source(baseline_source)

                if not baseline_storage_uri:
                    baseline_missing_reason = "No cloud-readable baseline source clip is available for this assimilated analysis."
                    if payload.strict_reference_baseline:
                        logger.warning(
                            "strict_baseline_downgraded drummer=%s stage=%s reason=%s",
                            target_slug,
                            stage,
                            baseline_missing_reason,
                        )
                else:
                    try:
                        baseline_ref = _ensure_reference_baseline_run(
                            db,
                            drummer_slug=target_slug,
                            baseline_source=baseline_source,
                            base_groove_id=item_base_groove_id,
                            sample_pack_version=payload.sample_pack_version,
                        )
                    except Exception as baseline_exc:
                        logger.exception(
                            "baseline_reference_create_failed drummer=%s source=%s",
                            target_slug,
                            baseline_source_debug,
                        )
                        if payload.strict_reference_baseline:
                            logger.warning(
                                "strict_baseline_downgraded drummer=%s stage=%s reason=%s",
                                target_slug,
                                stage,
                                str(baseline_exc),
                            )
                        baseline_missing_reason = str(baseline_exc)

                if payload.strict_reference_baseline and not baseline_ref:
                    if not baseline_missing_reason:
                        baseline_missing_reason = "Strict baseline requested but baseline reference artifact could not be created. Proceeding with A/B queueing."
                    logger.warning(
                        "strict_baseline_downgraded drummer=%s stage=%s reason=%s",
                        target_slug,
                        stage,
                        baseline_missing_reason,
                    )

                if baseline_ref:
                    baseline_run_id = str(baseline_ref.get("run_id") or "").strip() or None
                    reference_artifact_id = str(baseline_ref.get("artifact_id") or "").strip() or None

        # Do not synthesize a fake baseline when the original source clip is unavailable.
        # The page can still queue A/B candidates and mark the baseline lane as not required.
        generate_baseline = False
        requested = payload.candidate_count
        for idx in range(requested):
            seed_offset = idx + (1 if baseline_run_id else 0)
            seed_value = payload.seed if payload.seed is not None else (1000 + seed_offset)
            stage = "candidate_generate"
            try:
                run_data = generate_candidate_run(
                    db=db,
                    base_groove_id=effective_base_groove_id,
                    drummer_slug=target_slug,
                    seed=int(seed_value),
                    generation_controls=generation_controls,
                )
            except RuntimeError as gen_exc:
                message = str(gen_exc)
                if "rollup" in message.lower():
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "stage": stage,
                            "message": message,
                            "reason": "phase5_rollup_missing_or_unreadable",
                            "assimilationStatus": assimilation,
                            "runtime": runtime,
                        },
                    )
                raise

            run_metadata = {
                "requested_via": "generate-candidates",
                "candidate_index": idx,
                "render_profile_id": payload.render_profile_id,
                "sample_pack_version": payload.sample_pack_version,
                "target_drummer_slug": target_slug,
                "tempo_bpm": run_data.tempo_bpm,
                "time_signature": run_data.time_signature,
                "kit_id": run_data.kit_id,
                "base_groove_path": run_data.base_groove_path,
                "generation_base_groove_id": effective_base_groove_id,
                "source_analysis_id": baseline_analysis_id,
                "generation_controls": generation_controls,
                **run_data.metadata,
            }

            stage = "calibration_run_log"
            run_id = db.log_calibration_run(
                drummer_slug=target_slug,
                outcome="queued",
                note_count=run_data.note_count,
                metadata=run_metadata,
                metrics={},
                comparison={},
            )
            if not run_id:
                _raise_stage_error("Failed to log calibration run")
            created_run_ids.append(run_id)

            stage = "run_version_upsert"
            version_ok = db.upsert_run_version(
                run_id=run_id,
                generator_version="candidate_generator_v1",
                feature_version="metrics_v1",
                rollup_version="phase5",
                sample_pack_version=payload.sample_pack_version,
                seed=int(seed_value),
                commit_hash=None,
            )
            if not version_ok:
                logger.warning(
                    "generate-candidates run_version_upsert skipped for run_id=%s (continuing)",
                    run_id,
                )

            # Store event stream placeholder so render pipeline has context.
            stage = "run_events_upsert"
            events_ok = db.upsert_calibration_run_events(
                run_id=run_id,
                drummer_slug=target_slug,
                event_stream=run_data.event_stream,
                tempo_bpm=run_data.tempo_bpm,
                time_signature=run_data.time_signature,
                bars=run_data.bars,
                source_type="generate_candidates_autogen",
            )
            if not events_ok:
                logger.warning(
                    "generate-candidates run_events_upsert skipped for run_id=%s (continuing)",
                    run_id,
                )

            # Trigger render pipeline immediately.
            render_request = RenderRequest(
                run_id=run_id,
                render_profile_id=payload.render_profile_id,
                sample_pack_version=payload.sample_pack_version,
                kit_id=run_data.kit_id or "default_kit",
                seed=int(seed_value),
                render_recipe={
                    "base_groove_id": effective_base_groove_id,
                    "target_drummer_slug": target_slug,
                    "render_profile_id": payload.render_profile_id,
                    "requested_via": "generate-candidates",
                    "source_analysis_id": baseline_analysis_id,
                    "generation_controls": generation_controls,
                    "performance_spec": run_data.performance_spec,
                    "sections": run_data.sections,
                    "tempo_bpm": run_data.tempo_bpm,
                },
            )
            stage = "render_start"
            try:
                render_service.render_run(render_request)
            except Exception as render_exc:
                try:
                    db.log_calibration_render_job(
                        run_id=run_id,
                        render_profile_id=payload.render_profile_id,
                        sample_pack_version=payload.sample_pack_version,
                        status="failed",
                        error_text=str(render_exc),
                    )
                except Exception:
                    pass
                _raise_stage_error(f"Failed to start render: {render_exc}")

        item_id: Optional[str] = None
        if session_id and (created_run_ids or baseline_run_id):
            created = created_run_ids.copy()
            generated_baseline_id: Optional[str] = created.pop(0) if generate_baseline and created else None
            if not baseline_run_id:
                baseline_run_id = generated_baseline_id
            candidate_a_run_id: Optional[str] = created.pop(0) if created else None
            candidate_b_run_id: Optional[str] = created.pop(0) if created else None
            if not reference_artifact_id:
                reference_artifact_id = _pick_reference_artifact_id(db, baseline_run_id=baseline_run_id)

            stage = "evaluation_item_create"
            item_id = db.create_evaluation_item(
                session_id=session_id,
                base_groove_id=item_base_groove_id,
                target_drummer_slug=target_slug,
                reference_artifact_id=reference_artifact_id,
                baseline_run_id=baseline_run_id,
                candidate_a_run_id=candidate_a_run_id,
                candidate_b_run_id=candidate_b_run_id,
                eval_mode="AB",
                ab_mapping={"A": candidate_a_run_id, "B": candidate_b_run_id},
            )
            if not item_id:
                _raise_stage_error("Failed to create evaluation item")

            if payload.wait_for_all_artifacts:
                stage = "artifact_wait"
                wait_start = time.perf_counter()
                timeout_s = max(30.0, float(payload.artifact_wait_timeout_sec))
                poll_s = max(0.5, float(payload.artifact_poll_interval_ms) / 1000.0)
                lane_progress: Dict[str, Any] = {}

                while True:
                    lane_progress = _collect_item_lane_progress(
                        db,
                        baseline_run_id=baseline_run_id,
                        candidate_a_run_id=candidate_a_run_id,
                        candidate_b_run_id=candidate_b_run_id,
                    )
                    if lane_progress.get("all_ready"):
                        break

                    elapsed = time.perf_counter() - wait_start
                    if elapsed >= timeout_s:
                        raise HTTPException(
                            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                            detail={
                                "stage": stage,
                                "message": "Timed out waiting for baseline/A/B artifacts",
                                "item_id": item_id,
                                "elapsed_sec": round(elapsed, 2),
                                "progress": lane_progress,
                                "runtime": runtime,
                            },
                        )
                    await asyncio.sleep(poll_s)

        return {
            "status": "queued",
            "run_ids": created_run_ids,
            "session_id": session_id,
            "item_id": item_id,
            "baseline_run_id": baseline_run_id,
            "reference_artifact_id": reference_artifact_id,
            "baseline_reference_available": bool(reference_artifact_id),
            "baseline_missing_reason": baseline_missing_reason,
            "request_behavior": {
                "strict_reference_baseline_requested": bool(payload.strict_reference_baseline),
                "strict_reference_baseline_hard_fail_enabled": False,
                "include_baseline_requested": bool(payload.include_baseline),
                "generated_synthetic_baseline": bool(generate_baseline),
                "baseline_source_analysis_id": baseline_analysis_id,
            },
            "runtime": runtime,
            "artifact_wait_enforced": bool(payload.wait_for_all_artifacts),
            "artifact_wait_timeout_sec": int(payload.artifact_wait_timeout_sec),
        }
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and "runtime" not in exc.detail:
            exc.detail["runtime"] = runtime
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "stage": stage,
                "message": str(exc),
                "runtime": runtime,
            },
        )

@router.get("/drummers/{slug}", response_model=DrummerDetailPayload)
async def get_drummer(slug: str, db: CentralDatabaseService = Depends(get_db_service)) -> DrummerDetailPayload:
    slug = (slug or "").strip()
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drummer slug")

    async def _db_call_with_timeout(func, *args, timeout: float = 6.0, default=None, **kwargs):
        try:
            return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout)
        except Exception:
            return default

    try:
        drummer_row = await _db_call_with_timeout(
            db.get_drummer,
            slug,
            timeout=4.0,
            default={"id": slug, "display_name": slug},
        )
        if not drummer_row:
            drummer_row = {"id": slug, "display_name": slug}
        display_name = _display_name_from_row(drummer_row, slug)

        adjustments_record = await _db_call_with_timeout(
            db.get_calibration_adjustments,
            slug,
            timeout=5.0,
            default={},
        ) or {}
        adjustments: Dict[str, Any] = _safe_json_dict(adjustments_record.get("adjustments"))
        metadata: Dict[str, Any] = _safe_json_dict(adjustments_record.get("metadata"))

        rollup_targets = await _db_call_with_timeout(
            db.get_drummer_profile_rollup,
            drummer_slug=slug,
            timeout=6.0,
            default={},
        ) or {}
        if not isinstance(rollup_targets, dict):
            rollup_targets = {}

        latest_run = await _db_call_with_timeout(
            db.get_latest_calibration_run,
            drummer_slug=slug,
            timeout=6.0,
            default=None,
        )
        metrics = latest_run.metrics if latest_run and isinstance(latest_run.metrics, dict) else {}

        runs = await _db_call_with_timeout(
            db.get_calibration_runs,
            drummer_slug=slug,
            limit=10,
            timeout=6.0,
            default=[],
        ) or []
        run_history: List[CalibrationRunPayload] = []
        for run in runs:
            try:
                run_history.append(_serialize_run(run))
            except Exception:
                continue

        feedback_entries = await _db_call_with_timeout(
            db.get_calibration_feedback,
            drummer_slug=slug,
            limit=25,
            timeout=6.0,
            default=[],
        ) or []
        feedback_samples: List[FeedbackEntry] = []
        for item in feedback_entries:
            try:
                feedback_samples.append(_serialize_feedback(item))
            except Exception:
                continue

        completion_status = _completion_from_run(latest_run)

        assimilation_status = await _db_call_with_timeout(
            _assimilation_status_for_slug,
            db,
            slug,
            timeout=7.0,
            default=None,
        )
        if not assimilation_status:
            assimilation_status = {
                "status": "unknown",
                "ready_for_calibration": False,
                "missing_steps": ["ingestion"],
                "counts": {
                    "songs": 0,
                    "artifacts": 0,
                    "stems": 0,
                    "hit_events": 0,
                    "fills": 0,
                    "techniques": 0,
                },
                "metrics": {
                    "phase4_enriched_analyses": 0,
                    "phase5_rollups": 0,
                    "phase6_presets": 0,
                },
            }

        return DrummerDetailPayload(
            slug=slug,
            displayName=display_name,
            adjustments=adjustments,
            rollupTargets=rollup_targets,
            metrics=metrics,
            metadata=metadata,
            assimilationStatus=assimilation_status,
            runHistory=run_history,
            feedbackSamples=feedback_samples,
            completionStatus=completion_status,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/drummers/{slug}/adjustments")
async def update_adjustments(
    slug: str,
    payload: AdjustmentPayload,
    db: CentralDatabaseService = Depends(get_db_service),
) -> Dict[str, Any]:
    slug = (slug or "").strip()
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drummer slug")

    try:
        ok = db.upsert_calibration_adjustments(
            drummer_slug=slug,
            adjustments=payload.adjustments,
            metadata=payload.metadata,
        )
        if not ok:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save adjustments")

        record = db.get_calibration_adjustments(slug) or {}
        adjustments = _safe_json_dict(record.get("adjustments"))
        metadata = _safe_json_dict(record.get("metadata"))

        return {
            "status": "ok",
            "adjustments": adjustments,
            "metadata": metadata,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/drummers/{slug}/generate")
async def trigger_generation(
    slug: str,
    background_tasks: BackgroundTasks,
    db: CentralDatabaseService = Depends(get_db_service),
) -> Dict[str, Any]:
    slug = (slug or "").strip()
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drummer slug")

    try:
        run_id_seed = str(uuid.uuid4())
        queue_metadata = {
            "queued": True,
            "queued_at": datetime.utcnow().isoformat(),
        }

        run_id = db.log_calibration_run(
            run_id=run_id_seed,
            drummer_slug=slug,
            outcome="pending",
            metadata=queue_metadata,
        )
        if not run_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to queue calibration run")

        background_tasks.add_task(_complete_generation_run, slug=slug, run_id=run_id)

        return {
            "status": "queued",
            "run_id": run_id,
            "rollupSaved": False,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/feedback")
async def submit_feedback(
    payload: FeedbackSubmitRequest,
    db: CentralDatabaseService = Depends(get_db_service),
) -> Dict[str, Any]:
    slug = (payload.drummer or "").strip()
    if not slug:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing drummer slug")

    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Feedback comment required")

    try:
        metadata: Dict[str, Any] = {}
        if payload.run_id:
            metadata["run_id"] = payload.run_id.strip()
        if payload.item_id:
            metadata["item_id"] = payload.item_id.strip()

        feedback_id = db.log_calibration_feedback(
            drummer_slug=slug,
            rating=payload.rating,
            comment=comment,
            author=(payload.author or "Guest").strip() or "Guest",
            metadata=metadata if metadata else None,
        )

        if not feedback_id:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to record feedback")

        latest = db.get_calibration_feedback(drummer_slug=slug, limit=25)
        return {
            "status": "ok",
            "feedback_id": feedback_id,
            "feedback": [_serialize_feedback(item) for item in latest],
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

app.include_router(router)
app.include_router(calibration_v2_router)
app.include_router(assimilation_generation_router)

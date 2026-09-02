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
from backend.studiomind_trackai_api import router as studiomind_trackai_router
from backend.studiomind_trackai_generation_api import (
    router as studiomind_trackai_generation_router,
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
    async def _mvëÝí¢G§²ÚîÆ­yÙ½¹”¤°(€€€±¥µ¥Ðè¥¹Ð€ôEÕ•Éä¡‘•™…Õ±ÐôÈÀÀ°”ôÄ°±”ôÔÀÀÀ¤°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø…±¥‰É…Ñ¥½¹QÉ…¥¹¥¹áÁ½ÉÑA…å±½…è(€€€Í±Õ}™¥±Ñ•È€ô€¡‘ÉÕµµ•É}Í±Õœ½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥Ñ•µÌè1¥ÍÑm¥ÑmÍÑÈ°¹åut€ômt((€€€ÑÉäè(€€€€€€€•¹¥¹”€ô}É•ÅÕ¥É•}Á½ÍÑÉ•Í}•¹¥¹”¡‘ˆ¤(€€€€€€€Ý¥Ñ •¹¥¹”¹½¹¹•Ð ¤…Ì½¹¹}Áœè(€€€€€€€€€€€É½ÝÌ€ô½¹¹}Áœ¹•á•ÕÑ” (€€€€€€€€€€€€€€€Ñ•áÐ (€€€€€€€€€€€€€€€€€€€€ˆˆˆ(€€€€€€€€€€€€€€€€€€€M1P€¨(€€€€€€€€€€€€€€€€€€€I=4ÁÕ‰±¥Œ¹•Ù…±Õ…Ñ¥½¹}¥Ñ•µÌ(€€€€€€€€€€€€€€€€€€€]!I€ éÍ±Õ}™¥±Ñ•È€ô€œœ=HÑ…É•Ñ}‘ÉÕµµ•É}Í±Õœ€ô€éÍ±Õ}™¥±Ñ•È¤(€€€€€€€€€€€€€€€€€€€=IH	dÉ•…Ñ•‘}…ÐM(€€€€€€€€€€€€€€€€€€€1%5%P€é±¥µ¥Ð(€€€€€€€€€€€€€€€€€€€€ˆˆˆ(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€ì‰Í±Õ}™¥±Ñ•ÈˆèÍ±Õ}™¥±Ñ•È°€‰±¥µ¥Ðˆè¥¹Ð¡±¥µ¥Ð¥ô°(€€€€€€€€€€€€¤¹µ…ÁÁ¥¹Ì ¤¹…±° ¤((€€€€€€€™½ÈÉ½Ü¥¸É½ÝÌè(€€€€€€€€€€€¥Ñ•´€ô‘ˆ¹}É½Ý}Ñ½}•Ù…±Õ…Ñ¥½¹}¥Ñ•´¡É½Ü¤(€€€€€€€€€€€¥˜¹½Ð¥Ñ•´è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€€€€€¥Ñ•µ}Á…å±½…è¥ÑmÍÑÈ°¹åt€ôì(€€€€€€€€€€€€€€€€‰¥Ñ•´ˆèì(€€€€€€€€€€€€€€€€€€€€‰¥Ñ•µ}¥ˆè¥Ñ•´¹¥Ñ•µ}¥°(€€€€€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆè¥Ñ•´¹Í•ÍÍ¥½¹}¥°(€€€€€€€€€€€€€€€€€€€€‰Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœˆè¥Ñ•´¹Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€‰‰…Í•}É½½Ù•}¥ˆè¥Ñ•´¹‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€€€€€€€€€€‰É•™•É•¹•}…ÉÑ¥™…Ñ}¥ˆè¥Ñ•´¹É•™•É•¹•}…ÉÑ¥™…Ñ}¥°(€€€€€€€€€€€€€€€€€€€€‰‰…Í•±¥¹•}ÉÕ¹}¥ˆè¥Ñ•´¹‰…Í•±¥¹•}ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}…}ÉÕ¹}¥ˆè¥Ñ•´¹…¹‘¥‘…Ñ•}…}ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥ˆè¥Ñ•´¹…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€‰•Ù…±}µ½‘”ˆè¥Ñ•´¹•Ù…±}µ½‘”°(€€€€€€€€€€€€€€€€€€€€‰…‰}µ…ÁÁ¥¹œˆè¥Ñ•´¹…‰}µ…ÁÁ¥¹œ°(€€€€€€€€€€€€€€€€€€€€‰É•…Ñ•‘}…Ðˆè¥Ñ•´¹É•…Ñ•‘}…Ð¹¥Í½™½Éµ…Ð ¤¥˜¥Ñ•´¹É•…Ñ•‘}…Ð•±Í”9½¹”°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€‰…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÌˆè}…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÍ}™½É}Í±Õœ¡‘ˆ°¥Ñ•´¹Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœ¤°(€€€€€€€€€€€€€€€€‰Á…¥ÉÝ¥Í•}©Õ‘µ•¹ÑÌˆè}™•Ñ¡}Á…¥ÉÝ¥Í•}©Õ‘µ•¹ÑÌ¡‘ˆ°¥Ñ•µ}¥õ¥Ñ•´¹¥Ñ•µ}¥¤°(€€€€€€€€€€€€€€€€‰…ÑÑÉ¥‰ÕÑ•}É…Ñ¥¹Ìˆè}™•Ñ¡}…ÑÑÉ¥‰ÕÑ•}É…Ñ¥¹Ì¡‘ˆ°¥Ñ•µ}¥õ¥Ñ•´¹¥Ñ•µ}¥¤°(€€€€€€€€€€€€€€€€‰™••‘‰…¬ˆè}™•Ñ¡}¥Ñ•µ}™••‘‰…¬ (€€€€€€€€€€€€€€€€€€€‘ˆ°(€€€€€€€€€€€€€€€€€€€¥Ñ•µ}¥õ¥Ñ•´¹¥Ñ•µ}¥°(€€€€€€€€€€€€€€€€€€€‘ÉÕµµ•É}Í±Õœõ¥Ñ•´¹Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœ°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€€‰ÉÕ¹Ìˆèì(€€€€€€€€€€€€€€€€€€€€‰‰…Í•±¥¹”ˆè}Í•É¥…±¥é•}ÉÕ¹}‰Õ¹‘±”¡‘ˆ°¥Ñ•´¹‰…Í•±¥¹•}ÉÕ¹}¥¤°(€€€€€€€€€€€€€€€€€€€€‰ˆè}Í•É¥…±¥é•}ÉÕ¹}‰Õ¹‘±”¡‘ˆ°¥Ñ•´¹…¹‘¥‘…Ñ•}…}ÉÕ¹}¥¤°(€€€€€€€€€€€€€€€€€€€€‰ˆè}Í•É¥…±¥é•}ÉÕ¹}‰Õ¹‘±”¡‘ˆ°¥Ñ•´¹…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥¤°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€ô(€€€€€€€€€€€¥Ñ•µÌ¹…ÁÁ•¹¡¥Ñ•µ}Á…å±½…¤((€€€€€€€É•ÑÕÉ¸…±¥‰É…Ñ¥½¹QÉ…¥¹¥¹áÁ½ÉÑA…å±½… (€€€€€€€€€€€•áÁ½ÉÑ•‘}…Ðõ‘…Ñ•Ñ¥µ”¹ÕÑ¹½Ü ¤°(€€€€€€€€€€€¥Ñ•µ}½Õ¹Ðõ±•¸¡¥Ñ•µÌ¤°(€€€€€€€€€€€™¥±Ñ•ÉÌõì‰‘ÉÕµµ•É}Í±ÕœˆèÍ±Õ}™¥±Ñ•È½È9½¹”°€‰±¥µ¥Ðˆè¥¹Ð¡±¥µ¥Ð¥ô°(€€€€€€€€€€€¥Ñ•µÌõ¥Ñ•µÌ°(€€€€€€€€¤(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹Á½ÍÐ ˆ½•Ù…±Õ…Ñ¥½¸µ¥Ñ•µÌ½í¥Ñ•µ}¥‘ô½É…Ñ¥¹Ìˆ¤)…Íå¹Œ‘•˜ÍÕ‰µ¥Ñ}…ÑÑÉ¥‰ÕÑ•}É…Ñ¥¹Ì (€€€¥Ñ•µ}¥èÍÑÈ°(€€€Á…å±½…èÑÑÉ¥‰ÕÑ•I…Ñ¥¹ÍMÕ‰µ¥Ð°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø¥ÑmÍÑÈ°¹åtè(€€€¥Ñ•µ}¥€ô€¡¥Ñ•µ}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð¥Ñ•µ}¥è(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œ¥Ñ•´¥ˆ¤((€€€ÑÉäè(€€€€€€€É…Ñ¥¹}¥€ô‘ˆ¹±½}…ÑÑÉ¥‰ÕÑ•}É…Ñ¥¹œ¡¥Ñ•µ}¥õ¥Ñ•µ}¥°€¨©}µ½‘•±}‘ÕµÀ¡Á…å±½…¤¤(€€€€€€€¥˜¹½ÐÉ…Ñ¥¹}¥è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°ô‰…¥±•Ñ¼ÍÑ½É”É…Ñ¥¹Ìˆ¤(€€€€€€€É•ÑÕÉ¸ì‰ÍÑ…ÑÕÌˆè€‰½¬ˆ°€‰É…Ñ¥¹}¥ˆèÉ…Ñ¥¹}¥‘ô(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤()É½ÕÑ•È¹•Ð ˆ½•Ù…±Õ…Ñ¥½¸µ¥Ñ•µÌ½í¥Ñ•µ}¥‘ôˆ°É•ÍÁ½¹Í•}µ½‘•°õÙ…±Õ…Ñ¥½¹%Ñ•µA…å±½…¤)…Íå¹Œ‘•˜•Ñ}•Ù…±Õ…Ñ¥½¹}¥Ñ•´¡¥Ñ•µ}¥èÍÑÈ¤€´øÙ…±Õ…Ñ¥½¹%Ñ•µA…å±½…è(€€€¥Ñ•µ}¥€ô€¡¥Ñ•µ}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð¥Ñ•µ}¥è(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œ¥Ñ•´¥ˆ¤((€€€ÑÉäè(€€€€€€€‘ˆ€ô…Ý…¥Ð…Íå¹¥¼¹Ý…¥Ñ}™½È (€€€€€€€€€€€…Íå¹¥¼¹Ñ½}Ñ¡É•…¡•Ñ}‘‰}Í•ÉÙ¥”¤°(€€€€€€€€€€€Ñ¥µ•½ÕÐôÔ¸À°(€€€€€€€€¤(€€€€€€€¥Ñ•´€ô…Ý…¥Ð…Íå¹¥¼¹Ý…¥Ñ}™½È (€€€€€€€€€€€…Íå¹¥¼¹Ñ½}Ñ¡É•…¡‘ˆ¹•Ñ}•Ù…±Õ…Ñ¥½¹}¥Ñ•´°¥Ñ•µ}¥õ¥Ñ•µ}¥¤°(€€€€€€€€€€€Ñ¥µ•½ÕÐôà¸À°(€€€€€€€€¤(€€€€€€€¥˜¹½Ð¥Ñ•´è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÑ}9=Q}=U9°‘•Ñ…¥°ô‰Ù…±Õ…Ñ¥½¸¥Ñ•´¹½Ð™½Õ¹ˆ¤((€€€€€€€…ÉÑ¥™…Ñ}µ…Àè¥ÑmÍÑÈ°1¥ÍÑmÕ‘¥½ÉÑ¥™…ÑA…å±½…‘ut€ôíô(€€€€€€€™½È±…‰•°°ÉÕ¹}¥¥¸€ (€€€€€€€€€€€€ ‰‰…Í•±¥¹”ˆ°¥Ñ•´¹‰…Í•±¥¹•}ÉÕ¹}¥¤°(€€€€€€€€€€€€ ‰ˆ°¥Ñ•´¹…¹‘¥‘…Ñ•}…}ÉÕ¹}¥¤°(€€€€€€€€€€€€ ‰ˆ°¥Ñ•´¹…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥¤°(€€€€€€€€¤è(€€€€€€€€€€€ÉÕ¹}¥‘}Ù…°€ô€¡ÉÕ¹}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤¥˜ÉÕ¹}¥•±Í”€ˆˆ(€€€€€€€€€€€¥˜¹½ÐÉÕ¹}¥‘}Ù…°è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€…ÉÑ¥™…ÑÌ€ô…Ý…¥Ð…Íå¹¥¼¹Ý…¥Ñ}™½È (€€€€€€€€€€€€€€€€€€€…Íå¹¥¼¹Ñ½}Ñ¡É•…¡‘ˆ¹•Ñ}…Õ‘¥½}…ÉÑ¥™…ÑÍ}™½É}ÉÕ¸°ÉÕ¹}¥õÉÕ¹}¥‘}Ù…°¤°(€€€€€€€€€€€€€€€€€€€Ñ¥µ•½ÕÐôØ¸À°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ ‰•Ù…±Õ…Ñ¥½¹}¥Ñ•µ}…ÉÑ¥™…ÑÍ}±½½­ÕÁ}™…¥±•¥Ñ•µ}¥ô•ÌÉÕ¹}¥ô•Ìˆ°¥Ñ•µ}¥°ÉÕ¹}¥‘}Ù…°¤(€€€€€€€€€€€€€€€…ÉÑ¥™…ÑÌ€ômt(€€€€€€€€€€€…ÉÑ¥™…Ñ}µ…Ám±…‰•±t€ôm}Í•É¥…±¥é•}…ÉÑ¥™…Ð¡…ÉÑ¥™…Ð¤™½È…ÉÑ¥™…Ð¥¸…ÉÑ¥™…ÑÍt((€€€€€€€‰…Í•±¥¹•}±…‰•°€ô}¥¹™•É}‰…Í•±¥¹•}±…‰•°¡¥Ñ•´õ¥Ñ•´°…ÉÑ¥™…Ñ}±½½­ÕÀõ…ÉÑ¥™…Ñ}µ…À¤(€€€€€€€‰…Í•±¥¹•}É•™•É•¹•}…Õ‘¥½}ÕÉ°€ô}¥¹™•É}‰…Í•±¥¹•}É•™•É•¹•}…Õ‘¥½}ÕÉ° (€€€€€€€€€€€‘ˆ°(€€€€€€€€€€€¥Ñ•´õ¥Ñ•´°(€€€€€€€€€€€…ÉÑ¥™…Ñ}±½½­ÕÀõ…ÉÑ¥™…Ñ}µ…À°(€€€€€€€€¤(€€€€€€€É•ÑÕÉ¸}Í•É¥…±¥é•}¥Ñ•´ (€€€€€€€€€€€¥Ñ•´°(€€€€€€€€€€€…ÉÑ¥™…Ñ}µ…À°(€€€€€€€€€€€‰…Í•±¥¹•}±…‰•°õ‰…Í•±¥¹•}±…‰•°°(€€€€€€€€€€€‰…Í•±¥¹•}É•™•É•¹•}…Õ‘¥½}ÕÉ°õ‰…Í•±¥¹•}É•™•É•¹•}…Õ‘¥½}ÕÉ°°(€€€€€€€€¤(€€€•á•ÁÐQ¥µ•½ÕÑÉÉ½Èè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÑ}Q]e}Q%5=UP°‘•Ñ…¥°ô‰Ù…±Õ…Ñ¥½¸¥Ñ•´±½½­ÕÀÑ¥µ•½ÕÐˆ¤(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤()É½ÕÑ•È¹•Ð ˆ½Í•ÍÍ¥½¹Ì½¹•áÐˆ°É•ÍÁ½¹Í•}µ½‘•°õ=ÁÑ¥½¹…±mÙ…±Õ…Ñ¥½¹M•ÍÍ¥½¹A…å±½…‘t¤)…Íå¹Œ‘•˜•Ñ}¹•áÑ}Í•ÍÍ¥½¸ (€€€É•Ù¥•Ý•É}¥è=ÁÑ¥½¹…±mÍÑÉt€ôEÕ•Éä¡‘•™…Õ±Ðõ9½¹”¤°(€€€Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœè=ÁÑ¥½¹…±mÍÑÉt€ôEÕ•Éä¡‘•™…Õ±Ðõ9½¹”¤°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø=ÁÑ¥½¹…±mÙ…±Õ…Ñ¥½¹M•ÍÍ¥½¹A…å±½…‘tè(€€€ÑÉäè(€€€€€€€Í•ÍÍ¥½¸€ô‘ˆ¹•Ñ}¹•áÑ}•Ù…±Õ…Ñ¥½¹}Í•ÍÍ¥½¸ (€€€€€€€€€€€É•Ù¥•Ý•É}¥ô¡É•Ù¥•Ý•É}¥½È9½¹”¤°(€€€€€€€€€€€Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœô¡Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœ½È9½¹”¤°(€€€€€€€€¤(€€€€€€€¥˜¹½ÐÍ•ÍÍ¥½¸è(€€€€€€€€€€€É•ÑÕÉ¸9½¹”(€€€€€€€É•ÑÕÉ¸}Í•É¥…±¥é•}Í•ÍÍ¥½¸¡Í•ÍÍ¥½¸¤(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹Á½ÍÐ ˆ½Í•ÍÍ¥½¹Ì½íÍ•ÍÍ¥½¹}¥‘ô½ÍÑ…ÉÐˆ¤)…Íå¹Œ‘•˜ÍÑ…ÉÑ}Í•ÍÍ¥½¸¡Í•ÍÍ¥½¹}¥èÍÑÈ°‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤¤€´ø¥ÑmÍÑÈ°¹åtè(€€€Í•ÍÍ¥½¹}¥€ô€¡Í•ÍÍ¥½¹}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÍ•ÍÍ¥½¹}¥è(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œÍ•ÍÍ¥½¸¥ˆ¤((€€€ÑÉäè(€€€€€€€½¬€ô‘ˆ¹ÍÑ…ÉÑ}•Ù…±Õ…Ñ¥½¹}Í•ÍÍ¥½¸¡Í•ÍÍ¥½¹}¥õÍ•ÍÍ¥½¹}¥¤(€€€€€€€¥˜¹½Ð½¬è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÑ}9=Q}=U9°‘•Ñ…¥°ô‰M•ÍÍ¥½¸¹½Ð™½Õ¹ˆ¤(€€€€€€€É•ÑÕÉ¸ì‰ÍÑ…ÑÕÌˆè€‰½¬ˆ°€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥‘ô(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤()É½ÕÑ•È¹•Ð ˆ½ÉÕ¹Ì½íÉÕ¹}¥‘ôˆ¤)…Íå¹Œ‘•˜•Ñ}ÉÕ¸¡ÉÕ¹}¥èÍÑÈ°‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤¤€´ø¥ÑmÍÑÈ°¹åtè(€€€ÉÕ¹}¥€ô€¡ÉÕ¹}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÉÕ¹}¥è(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œÉÕ¸¥ˆ¤((€€€ÑÉäè(€€€€€€€ÉÕ¸€ô‘ˆ¹•Ñ}…±¥‰É…Ñ¥½¹}ÉÕ¸¡ÉÕ¹}¥õÉÕ¹}¥¤(€€€€€€€¥˜¹½ÐÉÕ¸è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÑ}9=Q}=U9°‘•Ñ…¥°ô‰IÕ¸¹½Ð™½Õ¹ˆ¤((€€€€€€€Ù•ÉÍ¥½¸€ô‘ˆ¹•Ñ}ÉÕ¹}Ù•ÉÍ¥½¸¡ÉÕ¹}¥õÉÕ¹}¥¤(€€€€€€€…ÉÑ¥™…ÑÌ€ô‘ˆ¹•Ñ}…Õ‘¥½}…ÉÑ¥™…ÑÍ}™½É}ÉÕ¸¡ÉÕ¹}¥õÉÕ¹}¥¤((€€€€€€€Á…å±½…è¥ÑmÍÑÈ°¹åt€ôì(€€€€€€€€€€€€‰ÉÕ¸ˆè}µ½‘•±}‘ÕµÀ¡}Í•É¥…±¥é•}ÉÕ¸¡ÉÕ¸¤¤°(€€€€€€€€€€€€‰…ÉÑ¥™…ÑÌˆèm}µ½‘•±}‘ÕµÀ¡}Í•É¥…±¥é•}…ÉÑ¥™…Ð¡¥Ñ•´¤¤™½È¥Ñ•´¥¸…ÉÑ¥™…ÑÍt°(€€€€€€€ô(€€€€€€€Í•É¥…±¥é•‘}Ù•ÉÍ¥½¸€ô}Í•É¥…±¥é•}ÉÕ¹}Ù•ÉÍ¥½¸¡Ù•ÉÍ¥½¸¤(€€€€€€€¥˜Í•É¥…±¥é•‘}Ù•ÉÍ¥½¸è(€€€€€€€€€€€Á…å±½…‘l‰ÉÕ¹}Ù•ÉÍ¥½¸‰t€ô}µ½‘•±}‘ÕµÀ¡Í•É¥…±¥é•‘}Ù•ÉÍ¥½¸¤((€€€€€€€É•ÑÕÉ¸Á…å±½…(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹•Ð ˆ½ÉÕ¹Ì½íÉÕ¹}¥‘ô½…ÉÑ¥™…ÑÌˆ°É•ÍÁ½¹Í•}µ½‘•°õ1¥ÍÑmÕ‘¥½ÉÑ¥™…ÑA…å±½…‘t¤)…Íå¹Œ‘•˜±¥ÍÑ}ÉÕ¹}…ÉÑ¥™…ÑÌ¡ÉÕ¹}¥èÍÑÈ°‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤¤€´ø1¥ÍÑmÕ‘¥½ÉÑ¥™…ÑA…å±½…‘tè(€€€ÉÕ¹}¥€ô€¡ÉÕ¹}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÉÕ¹}¥è(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œÉÕ¸¥ˆ¤((€€€ÑÉäè(€€€€€€€…ÉÑ¥™…ÑÌ€ô‘ˆ¹•Ñ}…Õ‘¥½}…ÉÑ¥™…ÑÍ}™½É}ÉÕ¸¡ÉÕ¹}¥õÉÕ¹}¥¤(€€€€€€€É•ÑÕÉ¸m}Í•É¥…±¥é•}…ÉÑ¥™…Ð¡¥Ñ•´¤™½È¥Ñ•´¥¸…ÉÑ¥™…ÑÍt(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹Á½ÍÐ ˆ½•¹•É…Ñ”µ…¹‘¥‘…Ñ•Ìˆ¤)…Íå¹Œ‘•˜•¹•É…Ñ•}…¹‘¥‘…Ñ•Ì (€€€Á…å±½…è•¹•É…Ñ•…¹‘¥‘…Ñ•ÍI•ÅÕ•ÍÐ°(€€€É•ÅÕ•ÍÐèI•ÅÕ•ÍÐ°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø¥ÑmÍÑÈ°¹åtè(€€€‰…Í•}É½½Ù•}¥€ô€¡Á…å±½…¹‰…Í•}É½½Ù•}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€Ñ…É•Ñ}Í±Õœ€ô€¡Á…å±½…¹Ñ…É•Ñ}‘ÉÕµµ•É}Í±Õœ½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€ÉÕ¹Ñ¥µ”€ô}ÉÕ¹Ñ¥µ•}‘¥…¹½ÍÑ¥Ì ¤(€€€¥˜¹½Ð‰…Í•}É½½Ù•}¥½È¹½ÐÑ…É•Ñ}Í±Õœè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸ (€€€€€€€€€€€ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°(€€€€€€€€€€€‘•Ñ…¥°õì(€€€€€€€€€€€€€€€€‰ÍÑ…”ˆè€‰É•ÅÕ•ÍÑ}Ù…±¥‘…Ñ”ˆ°(€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€‰5¥ÍÍ¥¹œ‰…Í”É½½Ù”½È‘ÉÕµµ•Èˆ°(€€€€€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€ô°(€€€€€€€€¤((€€€ÑÉäè(€€€€€€€ÍÑ…”€ô€‰…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÌˆ((€€€€€€€±½•È¹¥¹™¼ (€€€€€€€€€€€€‰•¹•É…Ñ•}…¹‘¥‘…Ñ•Í}É•ÅÕ•ÍÐ‰Õ¥±ô•Ì¥¹ÍÑ…¹”ô•Ì½É¥¥¸ô•Ì¡½ÍÐô•Ì‘ÉÕµµ•Èô•Ì‰…Í•}É½½Ù”ô•ÌÍÑÉ¥Ðô•Ì¥¹±Õ‘•}‰…Í•±¥¹”ô•Ìˆ°(€€€€€€€€€€€ÉÕ¹Ñ¥µ”¹•Ð ‰…Á¥}‰Õ¥±‘}µ…É­•Èˆ¤°(€€€€€€€€€€€ÉÕ¹Ñ¥µ”¹•Ð ‰…Á¥}¥¹ÍÑ…¹•}¥ˆ¤°(€€€€€€€€€€€É•ÅÕ•ÍÐ¹¡•…‘•ÉÌ¹•Ð ‰½É¥¥¸ˆ¤°(€€€€€€€€€€€É•ÅÕ•ÍÐ¹¡•…‘•ÉÌ¹•Ð ‰¡½ÍÐˆ¤°(€€€€€€€€€€€Ñ…É•Ñ}Í±Õœ°(€€€€€€€€€€€‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€‰½½°¡Á…å±½…¹ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹”¤°(€€€€€€€€€€€‰½½°¡Á…å±½…¹¥¹±Õ‘•}‰…Í•±¥¹”¤°(€€€€€€€€¤((€€€€€€€‘•˜}É…¥Í•}ÍÑ…•}•ÉÉ½È¡µ•ÍÍ…”èÍÑÈ°€¨°ÍÑ…ÑÕÍ}½‘”è¥¹Ð€ôÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H¤€´ø9½¹”è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸ (€€€€€€€€€€€€€€€ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÍ}½‘”°(€€€€€€€€€€€€€€€‘•Ñ…¥°õì(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…”ˆèÍÑ…”°(€€€€€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆèµ•ÍÍ…”°(€€€€€€€€€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€¤((€€€€€€€€ŒMÑÉ¥Ð…Ñ¥¹œèÉ•ÅÕ¥É”™Õ±°…ÍÍ¥µ¥±…Ñ¥½¸É•…‘¥¹•ÍÌ‰•™½É”…¹ä•¹•É…Ñ¥½¸¸(€€€€€€€…ÍÍ¥µ¥±…Ñ¥½¸€ô}…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÍ}™½É}Í±Õœ¡‘ˆ°Ñ…É•Ñ}Í±Õœ¤(€€€€€€€¥˜¹½Ð…ÍÍ¥µ¥±…Ñ¥½¸¹•Ð ‰É•…‘å}™½É}…±¥‰É…Ñ¥½¸ˆ¤è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸ (€€€€€€€€€€€€€€€ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀå}=91%P°(€€€€€€€€€€€€€€€‘•Ñ…¥°õì(€€€€€€€€€€€€€€€€€€€€‰ÍÑ…”ˆèÍÑ…”°(€€€€€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€‰ÍÍ¥µ¥±…Ñ¥½¸¹½ÐÉ•…‘ä™½È…±¥‰É…Ñ¥½¸ˆ°(€€€€€€€€€€€€€€€€€€€€‰…ÍÍ¥µ¥±…Ñ¥½¹MÑ…ÑÕÌˆè…ÍÍ¥µ¥±…Ñ¥½¸°(€€€€€€€€€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€¤((€€€€€€€ÍÑ…”€ô€‰É•¹‘•É}Í•ÉÙ¥•}¥¹¥Ðˆ(€€€€€€€É•¹‘•É}Í•ÉÙ¥”€ô…±¥‰É…Ñ¥½¹I•¹‘•ÉM•ÉÙ¥”¡‘ˆ¤(€€€€€€€Í•ÍÍ¥½¹}¥è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”(€€€€€€€É•Ù¥•Ý•É}¥€ô€¡Á…å±½…¹É•Ù¥•Ý•É}¥½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€¥˜É•Ù¥•Ý•É}¥è(€€€€€€€€€€€ÍÑ…”€ô€‰É•Ù¥•Ý•É}ÁÉ½™¥±•}ÕÁÍ•ÉÐˆ(€€€€€€€€€€€É•Ù¥•Ý•É}½¬€ô‘ˆ¹ÕÁÍ•ÉÑ}É•Ù¥•Ý•É}ÁÉ½™¥±”¡É•Ù¥•Ý•É}¥õÉ•Ù¥•Ý•É}¥°‘¥ÍÁ±…å}¹…µ”õÉ•Ù¥•Ý•É}¥¤(€€€€€€€€€€€¥˜¹½ÐÉ•Ù¥•Ý•É}½¬è(€€€€€€€€€€€€€€€}É…¥Í•}ÍÑ…•}•ÉÉ½È ‰…¥±•Ñ¼ÕÁÍ•ÉÐÉ•Ù¥•Ý•ÈÁÉ½™¥±”ˆ¤((€€€€€€€€€€€ÍÑ…”€ô€‰•Ù…±Õ…Ñ¥½¹}Í•ÍÍ¥½¹}É•…Ñ”ˆ(€€€€€€€€€€€Í•ÍÍ¥½¹}¥€ô‘ˆ¹É•…Ñ•}•Ù…±Õ…Ñ¥½¹}Í•ÍÍ¥½¸ (€€€€€€€€€€€€€€€É•Ù¥•Ý•É}¥õÉ•Ù¥•Ý•É}¥°(€€€€€€€€€€€€€€€Ñ…É•Ñ}‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€…ÁÁ}Ù•ÉÍ¥½¸õ˜‰…±¥‰É…Ñ¥½¹}Á¡…Í”Èéí1%	IQ%=9}A%}	U%1}5I-Iôˆ°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÍ•ÍÍ¥½¹}¥è(€€€€€€€€€€€€€€€}É…¥Í•}ÍÑ…•}•ÉÉ½È ‰…¥±•Ñ¼É•…Ñ”•Ù…±Õ…Ñ¥½¸Í•ÍÍ¥½¸ˆ¤((€€€€€€€É•…Ñ•‘}ÉÕ¹}¥‘Ìè1¥ÍÑmÍÑÉt€ômt(€€€€€€€•¹•É…Ñ¥½¹}½¹ÑÉ½±Ì€ôÁ…å±½…¹•¹•É…Ñ¥½¹}½¹ÑÉ½±Ì¥˜¥Í¥¹ÍÑ…¹”¡Á…å±½…¹•¹•É…Ñ¥½¹}½¹ÑÉ½±Ì°‘¥Ð¤•±Í”íô(€€€€€€€•™™•Ñ¥Ù•}‰…Í•}É½½Ù•}¥€ô‰…Í•}É½½Ù•}¥(€€€€€€€¥Ñ•µ}‰…Í•}É½½Ù•}¥€ô‰…Í•}É½½Ù•}¥(€€€€€€€‰…Í•±¥¹•}…¹…±åÍ¥Í}¥è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”(€€€€€€€‰…Í•±¥¹•}ÉÕ¹}¥è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”(€€€€€€€É•™•É•¹•}…ÉÑ¥™…Ñ}¥è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”(€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”((€€€€€€€¥˜Á…å±½…¹…¹‘¥‘…Ñ•}½Õ¹Ð€ð€Èè(€€€€€€€€€€€}É…¥Í•}ÍÑ…•}•ÉÉ½È ‰…¹‘¥‘…Ñ•}½Õ¹ÐµÕÍÐ‰”…Ð±•…ÍÐ€ÈÑ¼ÁÉ½‘Õ”½½ÕÑÁÕÑÌˆ°ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP¤((€€€€€€€¥˜Á…å±½…¹¥¹±Õ‘•}‰…Í•±¥¹”è(€€€€€€€€€€€ÍÑ…”€ô€‰‰…Í•±¥¹•}Í½ÕÉ•}Í•±•Ðˆ(€€€€€€€€€€€‰…Í•±¥¹•}Í½ÕÉ”€ô}Í•±•Ñ}…ÍÍ¥µ¥±…Ñ¥½¹}‰…Í•±¥¹•}Í½ÕÉ” (€€€€€€€€€€€€€€€‘ˆ°(€€€€€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€É•ÅÕ¥É•}±½Õ‘}ÕÉ¤õ…±Í”°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜Á…å±½…¹ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹”…¹¹½Ð‰…Í•±¥¹•}Í½ÕÉ”è(€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸€ô€‰9¼…ÍÍ¥µ¥±…Ñ•‰…Í•±¥¹”Í½ÕÉ”±¥À¥Ì…Ù…¥±…‰±”™½ÈÑ¡¥Ì‘ÉÕµµ•È¸AÉ½••‘¥¹œÝ¥Ñ ¹½¸µÍÑÉ¥Ð½ÅÕ•Õ•¥¹œ¸ˆ(€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ¥Ñ}‰…Í•±¥¹•}‘½Ý¹É…‘•‘ÉÕµµ•Èô•ÌÉ•…Í½¸ô•Ìˆ°(€€€€€€€€€€€€€€€€€€€Ñ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜‰…Í•±¥¹•}Í½ÕÉ”è(€€€€€€€€€€€€€€€Í½ÕÉ•}É½½Ù•}Á…Ñ €ôÍÑÈ¡‰…Í•±¥¹•}Í½ÕÉ”¹•Ð ‰‰…Í•}É½½Ù•}Á…Ñ ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜Í½ÕÉ•}É½½Ù•}Á…Ñ è(€€€€€€€€€€€€€€€€€€€•™™•Ñ¥Ù•}‰…Í•}É½½Ù•}¥€ôÍ½ÕÉ•}É½½Ù•}Á…Ñ (€€€€€€€€€€€€€€€…¹…±åÍ¥Í}¥€ôÍÑÈ¡‰…Í•±¥¹•}Í½ÕÉ”¹•Ð ‰…¹…±åÍ¥Í}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€€€€€€€€€€€€€¥˜…¹…±åÍ¥Í}¥è(€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}…¹…±åÍ¥Í}¥€ô…¹…±åÍ¥Í}¥(€€€€€€€€€€€€€€€€€€€¥Ñ•µ}‰…Í•}É½½Ù•}¥€ô˜‰…ÍÍ¥µ¥±…Ñ¥½¸éí…¹…±åÍ¥Í}¥‘ôˆ((€€€€€€€€€€€€€€€ÍÑ…”€ô€‰‰…Í•±¥¹•}É•™•É•¹•}ÉÕ¹}É•…Ñ”ˆ(€€€€€€€€€€€€€€€‰…Í•±¥¹•}É•˜è=ÁÑ¥½¹…±m¥ÑmÍÑÈ°¹åut€ô9½¹”(€€€€€€€€€€€€€€€‰…Í•±¥¹•}Í½ÕÉ•}‘•‰Õœ€ô}‰…Í•±¥¹•}Í½ÕÉ•}ÍÕµµ…Éä¡‰…Í•±¥¹•}Í½ÕÉ”¤(€€€€€€€€€€€€€€€‰…Í•±¥¹•}ÍÑ½É…•}ÕÉ¤€ô}É•™•É•¹•}ÍÑ½É…•}ÕÉ¥}™É½µ}‰…Í•±¥¹•}Í½ÕÉ”¡‰…Í•±¥¹•}Í½ÕÉ”¤((€€€€€€€€€€€€€€€¥˜¹½Ð‰…Í•±¥¹•}ÍÑ½É…•}ÕÉ¤è(€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸€ô€‰9¼±½ÕµÉ•…‘…‰±”‰…Í•±¥¹”Í½ÕÉ”±¥À¥Ì…Ù…¥±…‰±”™½ÈÑ¡¥Ì…ÍÍ¥µ¥±…Ñ•…¹…±åÍ¥Ì¸ˆ(€€€€€€€€€€€€€€€€€€€¥˜Á…å±½…¹ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹”è(€€€€€€€€€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ¥Ñ}‰…Í•±¥¹•}‘½Ý¹É…‘•‘ÉÕµµ•Èô•ÌÍÑ…”ô•ÌÉ•…Í½¸ô•Ìˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸°(€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€•±Í”è(€€€€€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}É•˜€ô}•¹ÍÕÉ•}É•™•É•¹•}‰…Í•±¥¹•}ÉÕ¸ (€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}Í½ÕÉ”õ‰…Í•±¥¹•}Í½ÕÉ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Í•}É½½Ù•}¥õ¥Ñ•µ}‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸õÁ…å±½…¹Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì‰…Í•±¥¹•}•áŒè(€€€€€€€€€€€€€€€€€€€€€€€±½•È¹•á•ÁÑ¥½¸ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰‰…Í•±¥¹•}É•™•É•¹•}É•…Ñ•}™…¥±•‘ÉÕµµ•Èô•ÌÍ½ÕÉ”ô•Ìˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}Í½ÕÉ•}‘•‰Õœ°(€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€€€€€¥˜Á…å±½…¹ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹”è(€€€€€€€€€€€€€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ¥Ñ}‰…Í•±¥¹•}‘½Ý¹É…‘•‘ÉÕµµ•Èô•ÌÍÑ…”ô•ÌÉ•…Í½¸ô•Ìˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€Ñ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑÈ¡‰…Í•±¥¹•}•áŒ¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸€ôÍÑÈ¡‰…Í•±¥¹•}•áŒ¤((€€€€€€€€€€€€€€€¥˜Á…å±½…¹ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹”…¹¹½Ð‰…Í•±¥¹•}É•˜è(€€€€€€€€€€€€€€€€€€€¥˜¹½Ð‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸è(€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸€ô€‰MÑÉ¥Ð‰…Í•±¥¹”É•ÅÕ•ÍÑ•‰ÕÐ‰…Í•±¥¹”É•™•É•¹”…ÉÑ¥™…Ð½Õ±¹½Ð‰”É•…Ñ•¸AÉ½••‘¥¹œÝ¥Ñ ½ÅÕ•Õ•¥¹œ¸ˆ(€€€€€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑÉ¥Ñ}‰…Í•±¥¹•}‘½Ý¹É…‘•‘ÉÕµµ•Èô•ÌÍÑ…”ô•ÌÉ•…Í½¸ô•Ìˆ°(€€€€€€€€€€€€€€€€€€€€€€€Ñ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…”°(€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸°(€€€€€€€€€€€€€€€€€€€€¤((€€€€€€€€€€€€€€€¥˜‰…Í•±¥¹•}É•˜è(€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}ÉÕ¹}¥€ôÍÑÈ¡‰…Í•±¥¹•}É•˜¹•Ð ‰ÉÕ¹}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤½È9½¹”(€€€€€€€€€€€€€€€€€€€É•™•É•¹•}…ÉÑ¥™…Ñ}¥€ôÍÑÈ¡‰…Í•±¥¹•}É•˜¹•Ð ‰…ÉÑ¥™…Ñ}¥ˆ¤½È€ˆˆ¤¹ÍÑÉ¥À ¤½È9½¹”((€€€€€€€€Œ¼¹½ÐÍå¹Ñ¡•Í¥é”„™…­”‰…Í•±¥¹”Ý¡•¸Ñ¡”½É¥¥¹…°Í½ÕÉ”±¥À¥ÌÕ¹…Ù…¥±…‰±”¸(€€€€€€€€ŒQ¡”Á…”…¸ÍÑ¥±°ÅÕ•Õ”½…¹‘¥‘…Ñ•Ì…¹µ…É¬Ñ¡”‰…Í•±¥¹”±…¹”…Ì¹½ÐÉ•ÅÕ¥É•¸(€€€€€€€•¹•É…Ñ•}‰…Í•±¥¹”€ô…±Í”(€€€€€€€É•ÅÕ•ÍÑ•€ôÁ…å±½…¹…¹‘¥‘…Ñ•}½Õ¹Ð(€€€€€€€™½È¥‘à¥¸É…¹”¡É•ÅÕ•ÍÑ•¤è(€€€€€€€€€€€Í••‘}½™™Í•Ð€ô¥‘à€¬€ Ä¥˜‰…Í•±¥¹•}ÉÕ¹}¥•±Í”€À¤(€€€€€€€€€€€Í••‘}Ù…±Õ”€ôÁ…å±½…¹Í••¥˜Á…å±½…¹Í••¥Ì¹½Ð9½¹”•±Í”€ ÄÀÀÀ€¬Í••‘}½™™Í•Ð¤(€€€€€€€€€€€ÍÑ…”€ô€‰…¹‘¥‘…Ñ•}•¹•É…Ñ”ˆ(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÉÕ¹}‘…Ñ„€ô•¹•É…Ñ•}…¹‘¥‘…Ñ•}ÉÕ¸ (€€€€€€€€€€€€€€€€€€€‘ˆõ‘ˆ°(€€€€€€€€€€€€€€€€€€€‰…Í•}É½½Ù•}¥õ•™™•Ñ¥Ù•}‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€Í••õ¥¹Ð¡Í••‘}Ù…±Õ”¤°(€€€€€€€€€€€€€€€€€€€•¹•É…Ñ¥½¹}½¹ÑÉ½±Ìõ•¹•É…Ñ¥½¹}½¹ÑÉ½±Ì°(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€•á•ÁÐIÕ¹Ñ¥µ•ÉÉ½È…Ì•¹}•áŒè(€€€€€€€€€€€€€€€µ•ÍÍ…”€ôÍÑÈ¡•¹}•áŒ¤(€€€€€€€€€€€€€€€¥˜€‰É½±±ÕÀˆ¥¸µ•ÍÍ…”¹±½Ý•È ¤è(€€€€€€€€€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸ (€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀå}=91%P°(€€€€€€€€€€€€€€€€€€€€€€€‘•Ñ…¥°õì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ…”ˆèÍÑ…”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆèµ•ÍÍ…”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰É•…Í½¸ˆè€‰Á¡…Í”Õ}É½±±ÕÁ}µ¥ÍÍ¥¹}½É}Õ¹É•…‘…‰±”ˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰…ÍÍ¥µ¥±…Ñ¥½¹MÑ…ÑÕÌˆè…ÍÍ¥µ¥±…Ñ¥½¸°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€É…¥Í”((€€€€€€€€€€€ÉÕ¹}µ•Ñ…‘…Ñ„€ôì(€€€€€€€€€€€€€€€€‰É•ÅÕ•ÍÑ•‘}Ù¥„ˆè€‰•¹•É…Ñ”µ…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€€€€€€€€€€‰…¹‘¥‘…Ñ•}¥¹‘•àˆè¥‘à°(€€€€€€€€€€€€€€€€‰É•¹‘•É}ÁÉ½™¥±•}¥ˆèÁ…å±½…¹É•¹‘•É}ÁÉ½™¥±•}¥°(€€€€€€€€€€€€€€€€‰Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸ˆèÁ…å±½…¹Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€€‰Ñ…É•Ñ}‘ÉÕµµ•É}Í±ÕœˆèÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€‰Ñ•µÁ½}‰Á´ˆèÉÕ¹}‘…Ñ„¹Ñ•µÁ½}‰Á´°(€€€€€€€€€€€€€€€€‰Ñ¥µ•}Í¥¹…ÑÕÉ”ˆèÉÕ¹}‘…Ñ„¹Ñ¥µ•}Í¥¹…ÑÕÉ”°(€€€€€€€€€€€€€€€€‰­¥Ñ}¥ˆèÉÕ¹}‘…Ñ„¹­¥Ñ}¥°(€€€€€€€€€€€€€€€€‰‰…Í•}É½½Ù•}Á…Ñ ˆèÉÕ¹}‘…Ñ„¹‰…Í•}É½½Ù•}Á…Ñ °(€€€€€€€€€€€€€€€€‰•¹•É…Ñ¥½¹}‰…Í•}É½½Ù•}¥ˆè•™™•Ñ¥Ù•}‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€€€€€€‰Í½ÕÉ•}…¹…±åÍ¥Í}¥ˆè‰…Í•±¥¹•}…¹…±åÍ¥Í}¥°(€€€€€€€€€€€€€€€€‰•¹•É…Ñ¥½¹}½¹ÑÉ½±Ìˆè•¹•É…Ñ¥½¹}½¹ÑÉ½±Ì°(€€€€€€€€€€€€€€€€¨©ÉÕ¹}‘…Ñ„¹µ•Ñ…‘…Ñ„°(€€€€€€€€€€€ô((€€€€€€€€€€€ÍÑ…”€ô€‰…±¥‰É…Ñ¥½¹}ÉÕ¹}±½œˆ(€€€€€€€€€€€ÉÕ¹}¥€ô‘ˆ¹±½}…±¥‰É…Ñ¥½¹}ÉÕ¸ (€€€€€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€½ÕÑ½µ”ô‰ÅÕ•Õ•ˆ°(€€€€€€€€€€€€€€€¹½Ñ•}½Õ¹ÐõÉÕ¹}‘…Ñ„¹¹½Ñ•}½Õ¹Ð°(€€€€€€€€€€€€€€€µ•Ñ…‘…Ñ„õÉÕ¹}µ•Ñ…‘…Ñ„°(€€€€€€€€€€€€€€€µ•ÑÉ¥Ìõíô°(€€€€€€€€€€€€€€€½µÁ…É¥Í½¸õíô°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÉÕ¹}¥è(€€€€€€€€€€€€€€€}É…¥Í•}ÍÑ…•}•ÉÉ½È ‰…¥±•Ñ¼±½œ…±¥‰É…Ñ¥½¸ÉÕ¸ˆ¤(€€€€€€€€€€€É•…Ñ•‘}ÉÕ¹}¥‘Ì¹…ÁÁ•¹¡ÉÕ¹}¥¤((€€€€€€€€€€€ÍÑ…”€ô€‰ÉÕ¹}Ù•ÉÍ¥½¹}ÕÁÍ•ÉÐˆ(€€€€€€€€€€€Ù•ÉÍ¥½¹}½¬€ô‘ˆ¹ÕÁÍ•ÉÑ}ÉÕ¹}Ù•ÉÍ¥½¸ (€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€•¹•É…Ñ½É}Ù•ÉÍ¥½¸ô‰…¹‘¥‘…Ñ•}•¹•É…Ñ½É}ØÄˆ°(€€€€€€€€€€€€€€€™•…ÑÕÉ•}Ù•ÉÍ¥½¸ô‰µ•ÑÉ¥Í}ØÄˆ°(€€€€€€€€€€€€€€€É½±±ÕÁ}Ù•ÉÍ¥½¸ô‰Á¡…Í”Ôˆ°(€€€€€€€€€€€€€€€Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸õÁ…å±½…¹Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€Í••õ¥¹Ð¡Í••‘}Ù…±Õ”¤°(€€€€€€€€€€€€€€€½µµ¥Ñ}¡…Í õ9½¹”°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½ÐÙ•ÉÍ¥½¹}½¬è(€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€€€€€€€€€€€€€‰•¹•É…Ñ”µ…¹‘¥‘…Ñ•ÌÉÕ¹}Ù•ÉÍ¥½¹}ÕÁÍ•ÉÐÍ­¥ÁÁ•™½ÈÉÕ¹}¥ô•Ì€¡½¹Ñ¥¹Õ¥¹œ¤ˆ°(€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€€¤((€€€€€€€€€€€€ŒMÑ½É”•Ù•¹ÐÍÑÉ•…´Á±…•¡½±‘•ÈÍ¼É•¹‘•ÈÁ¥Á•±¥¹”¡…Ì½¹Ñ•áÐ¸(€€€€€€€€€€€ÍÑ…”€ô€‰ÉÕ¹}•Ù•¹ÑÍ}ÕÁÍ•ÉÐˆ(€€€€€€€€€€€•Ù•¹ÑÍ}½¬€ô‘ˆ¹ÕÁÍ•ÉÑ}…±¥‰É…Ñ¥½¹}ÉÕ¹}•Ù•¹ÑÌ (€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€•Ù•¹Ñ}ÍÑÉ•…´õÉÕ¹}‘…Ñ„¹•Ù•¹Ñ}ÍÑÉ•…´°(€€€€€€€€€€€€€€€Ñ•µÁ½}‰Á´õÉÕ¹}‘…Ñ„¹Ñ•µÁ½}‰Á´°(€€€€€€€€€€€€€€€Ñ¥µ•}Í¥¹…ÑÕÉ”õÉÕ¹}‘…Ñ„¹Ñ¥µ•}Í¥¹…ÑÕÉ”°(€€€€€€€€€€€€€€€‰…ÉÌõÉÕ¹}‘…Ñ„¹‰…ÉÌ°(€€€€€€€€€€€€€€€Í½ÕÉ•}ÑåÁ”ô‰•¹•É…Ñ•}…¹‘¥‘…Ñ•Í}…ÕÑ½•¸ˆ°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½Ð•Ù•¹ÑÍ}½¬è(€€€€€€€€€€€€€€€±½•È¹Ý…É¹¥¹œ (€€€€€€€€€€€€€€€€€€€€‰•¹•É…Ñ”µ…¹‘¥‘…Ñ•ÌÉÕ¹}•Ù•¹ÑÍ}ÕÁÍ•ÉÐÍ­¥ÁÁ•™½ÈÉÕ¹}¥ô•Ì€¡½¹Ñ¥¹Õ¥¹œ¤ˆ°(€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥°(€€€€€€€€€€€€€€€€¤((€€€€€€€€€€€€ŒQÉ¥•ÈÉ•¹‘•ÈÁ¥Á•±¥¹”¥µµ•‘¥…Ñ•±ä¸(€€€€€€€€€€€É•¹‘•É}É•ÅÕ•ÍÐ€ôI•¹‘•ÉI•ÅÕ•ÍÐ (€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€É•¹‘•É}ÁÉ½™¥±•}¥õÁ…å±½…¹É•¹‘•É}ÁÉ½™¥±•}¥°(€€€€€€€€€€€€€€€Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸õÁ…å±½…¹Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€­¥Ñ}¥õÉÕ¹}‘…Ñ„¹­¥Ñ}¥½È€‰‘•™…Õ±Ñ}­¥Ðˆ°(€€€€€€€€€€€€€€€Í••õ¥¹Ð¡Í••‘}Ù…±Õ”¤°(€€€€€€€€€€€€€€€É•¹‘•É}É•¥Á”õì(€€€€€€€€€€€€€€€€€€€€‰‰…Í•}É½½Ù•}¥ˆè•™™•Ñ¥Ù•}‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€€€€€€€€€€‰Ñ…É•Ñ}‘ÉÕµµ•É}Í±ÕœˆèÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€€€€€€‰É•¹‘•É}ÁÉ½™¥±•}¥ˆèÁ…å±½…¹É•¹‘•É}ÁÉ½™¥±•}¥°(€€€€€€€€€€€€€€€€€€€€‰É•ÅÕ•ÍÑ•‘}Ù¥„ˆè€‰•¹•É…Ñ”µ…¹‘¥‘…Ñ•Ìˆ°(€€€€€€€€€€€€€€€€€€€€‰Í½ÕÉ•}…¹…±åÍ¥Í}¥ˆè‰…Í•±¥¹•}…¹…±åÍ¥Í}¥°(€€€€€€€€€€€€€€€€€€€€‰•¹•É…Ñ¥½¹}½¹ÑÉ½±Ìˆè•¹•É…Ñ¥½¹}½¹ÑÉ½±Ì°(€€€€€€€€€€€€€€€€€€€€‰Á•É™½Éµ…¹•}ÍÁ•ŒˆèÉÕ¹}‘…Ñ„¹Á•É™½Éµ…¹•}ÍÁ•Œ°(€€€€€€€€€€€€€€€€€€€€‰Í•Ñ¥½¹ÌˆèÉÕ¹}‘…Ñ„¹Í•Ñ¥½¹Ì°(€€€€€€€€€€€€€€€€€€€€‰Ñ•µÁ½}‰Á´ˆèÉÕ¹}‘…Ñ„¹Ñ•µÁ½}‰Á´°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€¤(€€€€€€€€€€€ÍÑ…”€ô€‰É•¹‘•É}ÍÑ…ÉÐˆ(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É•¹‘•É}Í•ÉÙ¥”¹É•¹‘•É}ÉÕ¸¡É•¹‘•É}É•ÅÕ•ÍÐ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…ÌÉ•¹‘•É}•áŒè(€€€€€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€€€€€‘ˆ¹±½}…±¥‰É…Ñ¥½¹}É•¹‘•É}©½ˆ (€€€€€€€€€€€€€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€É•¹‘•É}ÁÉ½™¥±•}¥õÁ…å±½…¹É•¹‘•É}ÁÉ½™¥±•}¥°(€€€€€€€€€€€€€€€€€€€€€€€Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸õÁ…å±½…¹Í…µÁ±•}Á…­}Ù•ÉÍ¥½¸°(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÌô‰™…¥±•ˆ°(€€€€€€€€€€€€€€€€€€€€€€€•ÉÉ½É}Ñ•áÐõÍÑÈ¡É•¹‘•É}•áŒ¤°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€€€€€Á…ÍÌ(€€€€€€€€€€€€€€€}É…¥Í•}ÍÑ…•}•ÉÉ½È¡˜‰…¥±•Ñ¼ÍÑ…ÉÐÉ•¹‘•ÈèíÉ•¹‘•É}•áôˆ¤((€€€€€€€¥Ñ•µ}¥è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”(€€€€€€€¥˜Í•ÍÍ¥½¹}¥…¹€¡É•…Ñ•‘}ÉÕ¹}¥‘Ì½È‰…Í•±¥¹•}ÉÕ¹}¥¤è(€€€€€€€€€€€É•…Ñ•€ôÉ•…Ñ•‘}ÉÕ¹}¥‘Ì¹½Áä ¤(€€€€€€€€€€€•¹•É…Ñ•‘}‰…Í•±¥¹•}¥è=ÁÑ¥½¹…±mÍÑÉt€ôÉ•…Ñ•¹Á½À À¤¥˜•¹•É…Ñ•}‰…Í•±¥¹”…¹É•…Ñ••±Í”9½¹”(€€€€€€€€€€€¥˜¹½Ð‰…Í•±¥¹•}ÉÕ¹}¥è(€€€€€€€€€€€€€€€‰…Í•±¥¹•}ÉÕ¹}¥€ô•¹•É…Ñ•‘}‰…Í•±¥¹•}¥(€€€€€€€€€€€…¹‘¥‘…Ñ•}…}ÉÕ¹}¥è=ÁÑ¥½¹…±mÍÑÉt€ôÉ•…Ñ•¹Á½À À¤¥˜É•…Ñ••±Í”9½¹”(€€€€€€€€€€€…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥è=ÁÑ¥½¹…±mÍÑÉt€ôÉ•…Ñ•¹Á½À À¤¥˜É•…Ñ••±Í”9½¹”(€€€€€€€€€€€¥˜¹½ÐÉ•™•É•¹•}…ÉÑ¥™…Ñ}¥è(€€€€€€€€€€€€€€€É•™•É•¹•}…ÉÑ¥™…Ñ}¥€ô}Á¥­}É•™•É•¹•}…ÉÑ¥™…Ñ}¥¡‘ˆ°‰…Í•±¥¹•}ÉÕ¹}¥õ‰…Í•±¥¹•}ÉÕ¹}¥¤((€€€€€€€€€€€ÍÑ…”€ô€‰•Ù…±Õ…Ñ¥½¹}¥Ñ•µ}É•…Ñ”ˆ(€€€€€€€€€€€¥Ñ•µ}¥€ô‘ˆ¹É•…Ñ•}•Ù…±Õ…Ñ¥½¹}¥Ñ•´ (€€€€€€€€€€€€€€€Í•ÍÍ¥½¹}¥õÍ•ÍÍ¥½¹}¥°(€€€€€€€€€€€€€€€‰…Í•}É½½Ù•}¥õ¥Ñ•µ}‰…Í•}É½½Ù•}¥°(€€€€€€€€€€€€€€€Ñ…É•Ñ}‘ÉÕµµ•É}Í±ÕœõÑ…É•Ñ}Í±Õœ°(€€€€€€€€€€€€€€€É•™•É•¹•}…ÉÑ¥™…Ñ}¥õÉ•™•É•¹•}…ÉÑ¥™…Ñ}¥°(€€€€€€€€€€€€€€€‰…Í•±¥¹•}ÉÕ¹}¥õ‰…Í•±¥¹•}ÉÕ¹}¥°(€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}…}ÉÕ¹}¥õ…¹‘¥‘…Ñ•}…}ÉÕ¹}¥°(€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥õ…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥°(€€€€€€€€€€€€€€€•Ù…±}µ½‘”ô‰ˆ°(€€€€€€€€€€€€€€€…‰}µ…ÁÁ¥¹œõì‰ˆè…¹‘¥‘…Ñ•}…}ÉÕ¹}¥°€‰ˆè…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥‘ô°(€€€€€€€€€€€€¤(€€€€€€€€€€€¥˜¹½Ð¥Ñ•µ}¥è(€€€€€€€€€€€€€€€}É…¥Í•}ÍÑ…•}•ÉÉ½È ‰…¥±•Ñ¼É•…Ñ”•Ù…±Õ…Ñ¥½¸¥Ñ•´ˆ¤((€€€€€€€€€€€¥˜Á…å±½…¹Ý…¥Ñ}™½É}…±±}…ÉÑ¥™…ÑÌè(€€€€€€€€€€€€€€€ÍÑ…”€ô€‰…ÉÑ¥™…Ñ}Ý…¥Ðˆ(€€€€€€€€€€€€€€€Ý…¥Ñ}ÍÑ…ÉÐ€ôÑ¥µ”¹Á•É™}½Õ¹Ñ•È ¤(€€€€€€€€€€€€€€€Ñ¥µ•½ÕÑ}Ì€ôµ…à ÌÀ¸À°™±½…Ð¡Á…å±½…¹…ÉÑ¥™…Ñ}Ý…¥Ñ}Ñ¥µ•½ÕÑ}Í•Œ¤¤(€€€€€€€€€€€€€€€Á½±±}Ì€ôµ…à À¸Ô°™±½…Ð¡Á…å±½…¹…ÉÑ¥™…Ñ}Á½±±}¥¹Ñ•ÉÙ…±}µÌ¤€¼€ÄÀÀÀ¸À¤(€€€€€€€€€€€€€€€±…¹•}ÁÉ½É•ÍÌè¥ÑmÍÑÈ°¹åt€ôíô((€€€€€€€€€€€€€€€Ý¡¥±”QÉÕ”è(€€€€€€€€€€€€€€€€€€€±…¹•}ÁÉ½É•ÍÌ€ô}½±±•Ñ}¥Ñ•µ}±…¹•}ÁÉ½É•ÍÌ (€€€€€€€€€€€€€€€€€€€€€€€‘ˆ°(€€€€€€€€€€€€€€€€€€€€€€€‰…Í•±¥¹•}ÉÕ¹}¥õ‰…Í•±¥¹•}ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}…}ÉÕ¹}¥õ…¹‘¥‘…Ñ•}…}ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€€€€…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥õ…¹‘¥‘…Ñ•}‰}ÉÕ¹}¥°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€¥˜±…¹•}ÁÉ½É•ÍÌ¹•Ð ‰…±±}É•…‘äˆ¤è(€€€€€€€€€€€€€€€€€€€€€€€‰É•…¬((€€€€€€€€€€€€€€€€€€€•±…ÁÍ•€ôÑ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´Ý…¥Ñ}ÍÑ…ÉÐ(€€€€€€€€€€€€€€€€€€€¥˜•±…ÁÍ•€øôÑ¥µ•½ÕÑ}Ìè(€€€€€€€€€€€€€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸ (€€€€€€€€€€€€€€€€€€€€€€€€€€€ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÑ}Q]e}Q%5=UP°(€€€€€€€€€€€€€€€€€€€€€€€€€€€‘•Ñ…¥°õì(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÍÑ…”ˆèÍÑ…”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€‰Q¥µ•½ÕÐÝ…¥Ñ¥¹œ™½È‰…Í•±¥¹”½½…ÉÑ¥™…ÑÌˆ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¥Ñ•µ}¥ˆè¥Ñ•µ}¥°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰•±…ÁÍ•‘}Í•ŒˆèÉ½Õ¹¡•±…ÁÍ•°€È¤°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÁÉ½É•ÍÌˆè±…¹•}ÁÉ½É•ÍÌ°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€€€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€…Ý…¥Ð…Íå¹¥¼¹Í±••À¡Á½±±}Ì¤((€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰ÅÕ•Õ•ˆ°(€€€€€€€€€€€€‰ÉÕ¹}¥‘ÌˆèÉ•…Ñ•‘}ÉÕ¹}¥‘Ì°(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥°(€€€€€€€€€€€€‰¥Ñ•µ}¥ˆè¥Ñ•µ}¥°(€€€€€€€€€€€€‰‰…Í•±¥¹•}ÉÕ¹}¥ˆè‰…Í•±¥¹•}ÉÕ¹}¥°(€€€€€€€€€€€€‰É•™•É•¹•}…ÉÑ¥™…Ñ}¥ˆèÉ•™•É•¹•}…ÉÑ¥™…Ñ}¥°(€€€€€€€€€€€€‰‰…Í•±¥¹•}É•™•É•¹•}…Ù…¥±…‰±”ˆè‰½½°¡É•™•É•¹•}…ÉÑ¥™…Ñ}¥¤°(€€€€€€€€€€€€‰‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸ˆè‰…Í•±¥¹•}µ¥ÍÍ¥¹}É•…Í½¸°(€€€€€€€€€€€€‰É•ÅÕ•ÍÑ}‰•¡…Ù¥½Èˆèì(€€€€€€€€€€€€€€€€‰ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹•}É•ÅÕ•ÍÑ•ˆè‰½½°¡Á…å±½…¹ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹”¤°(€€€€€€€€€€€€€€€€‰ÍÑÉ¥Ñ}É•™•É•¹•}‰…Í•±¥¹•}¡…É‘}™…¥±}•¹…‰±•ˆè…±Í”°(€€€€€€€€€€€€€€€€‰¥¹±Õ‘•}‰…Í•±¥¹•}É•ÅÕ•ÍÑ•ˆè‰½½°¡Á…å±½…¹¥¹±Õ‘•}‰…Í•±¥¹”¤°(€€€€€€€€€€€€€€€€‰•¹•É…Ñ•‘}Íå¹Ñ¡•Ñ¥}‰…Í•±¥¹”ˆè‰½½°¡•¹•É…Ñ•}‰…Í•±¥¹”¤°(€€€€€€€€€€€€€€€€‰‰…Í•±¥¹•}Í½ÕÉ•}…¹…±åÍ¥Í}¥ˆè‰…Í•±¥¹•}…¹…±åÍ¥Í}¥°(€€€€€€€€€€€ô°(€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€€‰…ÉÑ¥™…Ñ}Ý…¥Ñ}•¹™½É•ˆè‰½½°¡Á…å±½…¹Ý…¥Ñ}™½É}…±±}…ÉÑ¥™…ÑÌ¤°(€€€€€€€€€€€€‰…ÉÑ¥™…Ñ}Ý…¥Ñ}Ñ¥µ•½ÕÑ}Í•Œˆè¥¹Ð¡Á…å±½…¹…ÉÑ¥™…Ñ}Ý…¥Ñ}Ñ¥µ•½ÕÑ}Í•Œ¤°(€€€€€€€ô(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡•áŒ¹‘•Ñ…¥°°‘¥Ð¤…¹€‰ÉÕ¹Ñ¥µ”ˆ¹½Ð¥¸•áŒ¹‘•Ñ…¥°è(€€€€€€€€€€€•áŒ¹‘•Ñ…¥±l‰ÉÕ¹Ñ¥µ”‰t€ôÉÕ¹Ñ¥µ”(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸ (€€€€€€€€€€€ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°(€€€€€€€€€€€‘•Ñ…¥°õì(€€€€€€€€€€€€€€€€‰ÍÑ…”ˆèÍÑ…”°(€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆèÍÑÈ¡•áŒ¤°(€€€€€€€€€€€€€€€€‰ÉÕ¹Ñ¥µ”ˆèÉÕ¹Ñ¥µ”°(€€€€€€€€€€€ô°(€€€€€€€€¤()É½ÕÑ•È¹•Ð ˆ½‘ÉÕµµ•ÉÌ½íÍ±Õôˆ°É•ÍÁ½¹Í•}µ½‘•°õÉÕµµ•É•Ñ…¥±A…å±½…¤)…Íå¹Œ‘•˜•Ñ}‘ÉÕµµ•È¡Í±ÕœèÍÑÈ°‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤¤€´øÉÕµµ•É•Ñ…¥±A…å±½…è(€€€Í±Õœ€ô€¡Í±Õœ½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÍ±Õœè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œ‘ÉÕµµ•ÈÍ±Õœˆ¤((€€€…Íå¹Œ‘•˜}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ¡™Õ¹Œ°€©…ÉÌ°Ñ¥µ•½ÕÐè™±½…Ð€ô€Ø¸À°‘•™…Õ±Ðõ9½¹”°€¨©­Ý…ÉÌ¤è(€€€€€€€ÑÉäè(€€€€€€€€€€€É•ÑÕÉ¸…Ý…¥Ð…Íå¹¥¼¹Ý…¥Ñ}™½È¡…Íå¹¥¼¹Ñ½}Ñ¡É•…¡™Õ¹Œ°€©…ÉÌ°€¨©­Ý…ÉÌ¤°Ñ¥µ•½ÕÐõÑ¥µ•½ÕÐ¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€É•ÑÕÉ¸‘•™…Õ±Ð((€€€ÑÉäè(€€€€€€€‘ÉÕµµ•É}É½Ü€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€‘ˆ¹•Ñ}‘ÉÕµµ•È°(€€€€€€€€€€€Í±Õœ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôÐ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõì‰¥ˆèÍ±Õœ°€‰‘¥ÍÁ±…å}¹…µ”ˆèÍ±Õô°(€€€€€€€€¤(€€€€€€€¥˜¹½Ð‘ÉÕµµ•É}É½Üè(€€€€€€€€€€€‘ÉÕµµ•É}É½Ü€ôì‰¥ˆèÍ±Õœ°€‰‘¥ÍÁ±…å}¹…µ”ˆèÍ±Õô(€€€€€€€‘¥ÍÁ±…å}¹…µ”€ô}‘¥ÍÁ±…å}¹…µ•}™É½µ}É½Ü¡‘ÉÕµµ•É}É½Ü°Í±Õœ¤((€€€€€€€…‘©ÕÍÑµ•¹ÑÍ}É•½É€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€‘ˆ¹•Ñ}…±¥‰É…Ñ¥½¹}…‘©ÕÍÑµ•¹ÑÌ°(€€€€€€€€€€€Í±Õœ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôÔ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõíô°(€€€€€€€€¤½Èíô(€€€€€€€…‘©ÕÍÑµ•¹ÑÌè¥ÑmÍÑÈ°¹åt€ô}Í…™•}©Í½¹}‘¥Ð¡…‘©ÕÍÑµ•¹ÑÍ}É•½É¹•Ð ‰…‘©ÕÍÑµ•¹ÑÌˆ¤¤(€€€€€€€µ•Ñ…‘…Ñ„è¥ÑmÍÑÈ°¹åt€ô}Í…™•}©Í½¹}‘¥Ð¡…‘©ÕÍÑµ•¹ÑÍ}É•½É¹•Ð ‰µ•Ñ…‘…Ñ„ˆ¤¤((€€€€€€€É½±±ÕÁ}Ñ…É•ÑÌ€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€‘ˆ¹•Ñ}‘ÉÕµµ•É}ÁÉ½™¥±•}É½±±ÕÀ°(€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôØ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõíô°(€€€€€€€€¤½Èíô(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡É½±±ÕÁ}Ñ…É•ÑÌ°‘¥Ð¤è(€€€€€€€€€€€É½±±ÕÁ}Ñ…É•ÑÌ€ôíô((€€€€€€€±…Ñ•ÍÑ}ÉÕ¸€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€‘ˆ¹•Ñ}±…Ñ•ÍÑ}…±¥‰É…Ñ¥½¹}ÉÕ¸°(€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôØ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõ9½¹”°(€€€€€€€€¤(€€€€€€€µ•ÑÉ¥Ì€ô±…Ñ•ÍÑ}ÉÕ¸¹µ•ÑÉ¥Ì¥˜±…Ñ•ÍÑ}ÉÕ¸…¹¥Í¥¹ÍÑ…¹”¡±…Ñ•ÍÑ}ÉÕ¸¹µ•ÑÉ¥Ì°‘¥Ð¤•±Í”íô((€€€€€€€ÉÕ¹Ì€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€‘ˆ¹•Ñ}…±¥‰É…Ñ¥½¹}ÉÕ¹Ì°(€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€±¥µ¥ÐôÄÀ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôØ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõmt°(€€€€€€€€¤½Èmt(€€€€€€€ÉÕ¹}¡¥ÍÑ½Éäè1¥ÍÑm…±¥‰É…Ñ¥½¹IÕ¹A…å±½…‘t€ômt(€€€€€€€™½ÈÉÕ¸¥¸ÉÕ¹Ìè(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€ÉÕ¹}¡¥ÍÑ½Éä¹…ÁÁ•¹¡}Í•É¥…±¥é•}ÉÕ¸¡ÉÕ¸¤¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€™••‘‰…­}•¹ÑÉ¥•Ì€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€‘ˆ¹•Ñ}…±¥‰É…Ñ¥½¹}™••‘‰…¬°(€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€±¥µ¥ÐôÈÔ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôØ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõmt°(€€€€€€€€¤½Èmt(€€€€€€€™••‘‰…­}Í…µÁ±•Ìè1¥ÍÑm••‘‰…­¹ÑÉåt€ômt(€€€€€€€™½È¥Ñ•´¥¸™••‘‰…­}•¹ÑÉ¥•Ìè(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€™••‘‰…­}Í…µÁ±•Ì¹…ÁÁ•¹¡}Í•É¥…±¥é•}™••‘‰…¬¡¥Ñ•´¤¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€½µÁ±•Ñ¥½¹}ÍÑ…ÑÕÌ€ô}½µÁ±•Ñ¥½¹}™É½µ}ÉÕ¸¡±…Ñ•ÍÑ}ÉÕ¸¤((€€€€€€€…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÌ€ô…Ý…¥Ð}‘‰}…±±}Ý¥Ñ¡}Ñ¥µ•½ÕÐ (€€€€€€€€€€€}…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÍ}™½É}Í±Õœ°(€€€€€€€€€€€‘ˆ°(€€€€€€€€€€€Í±Õœ°(€€€€€€€€€€€Ñ¥µ•½ÕÐôÜ¸À°(€€€€€€€€€€€‘•™…Õ±Ðõ9½¹”°(€€€€€€€€¤(€€€€€€€¥˜¹½Ð…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÌè(€€€€€€€€€€€…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÌ€ôì(€€€€€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰Õ¹­¹½Ý¸ˆ°(€€€€€€€€€€€€€€€€‰É•…‘å}™½É}…±¥‰É…Ñ¥½¸ˆè…±Í”°(€€€€€€€€€€€€€€€€‰µ¥ÍÍ¥¹}ÍÑ•ÁÌˆèl‰¥¹•ÍÑ¥½¸‰t°(€€€€€€€€€€€€€€€€‰½Õ¹ÑÌˆèì(€€€€€€€€€€€€€€€€€€€€‰Í½¹Ìˆè€À°(€€€€€€€€€€€€€€€€€€€€‰…ÉÑ¥™…ÑÌˆè€À°(€€€€€€€€€€€€€€€€€€€€‰ÍÑ•µÌˆè€À°(€€€€€€€€€€€€€€€€€€€€‰¡¥Ñ}•Ù•¹ÑÌˆè€À°(€€€€€€€€€€€€€€€€€€€€‰™¥±±Ìˆè€À°(€€€€€€€€€€€€€€€€€€€€‰Ñ•¡¹¥ÅÕ•Ìˆè€À°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€€€€€€‰µ•ÑÉ¥Ìˆèì(€€€€€€€€€€€€€€€€€€€€‰Á¡…Í”Ñ}•¹É¥¡•‘}…¹…±åÍ•Ìˆè€À°(€€€€€€€€€€€€€€€€€€€€‰Á¡…Í”Õ}É½±±ÕÁÌˆè€À°(€€€€€€€€€€€€€€€€€€€€‰Á¡…Í”Ù}ÁÉ•Í•ÑÌˆè€À°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€ô((€€€€€€€É•ÑÕÉ¸ÉÕµµ•É•Ñ…¥±A…å±½… (€€€€€€€€€€€Í±ÕœõÍ±Õœ°(€€€€€€€€€€€‘¥ÍÁ±…å9…µ”õ‘¥ÍÁ±…å}¹…µ”°(€€€€€€€€€€€…‘©ÕÍÑµ•¹ÑÌõ…‘©ÕÍÑµ•¹ÑÌ°(€€€€€€€€€€€É½±±ÕÁQ…É•ÑÌõÉ½±±ÕÁ}Ñ…É•ÑÌ°(€€€€€€€€€€€µ•ÑÉ¥Ìõµ•ÑÉ¥Ì°(€€€€€€€€€€€µ•Ñ…‘…Ñ„õµ•Ñ…‘…Ñ„°(€€€€€€€€€€€…ÍÍ¥µ¥±…Ñ¥½¹MÑ…ÑÕÌõ…ÍÍ¥µ¥±…Ñ¥½¹}ÍÑ…ÑÕÌ°(€€€€€€€€€€€ÉÕ¹!¥ÍÑ½ÉäõÉÕ¹}¡¥ÍÑ½Éä°(€€€€€€€€€€€™••‘‰…­M…µÁ±•Ìõ™••‘‰…­}Í…µÁ±•Ì°(€€€€€€€€€€€½µÁ±•Ñ¥½¹MÑ…ÑÕÌõ½µÁ±•Ñ¥½¹}ÍÑ…ÑÕÌ°(€€€€€€€€¤(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹Á½ÍÐ ˆ½‘ÉÕµµ•ÉÌ½íÍ±Õô½…‘©ÕÍÑµ•¹ÑÌˆ¤)…Íå¹Œ‘•˜ÕÁ‘…Ñ•}…‘©ÕÍÑµ•¹ÑÌ (€€€Í±ÕœèÍÑÈ°(€€€Á…å±½…è‘©ÕÍÑµ•¹ÑA…å±½…°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø¥ÑmÍÑÈ°¹åtè(€€€Í±Õœ€ô€¡Í±Õœ½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÍ±Õœè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œ‘ÉÕµµ•ÈÍ±Õœˆ¤((€€€ÑÉäè(€€€€€€€½¬€ô‘ˆ¹ÕÁÍ•ÉÑ}…±¥‰É…Ñ¥½¹}…‘©ÕÍÑµ•¹ÑÌ (€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€…‘©ÕÍÑµ•¹ÑÌõÁ…å±½…¹…‘©ÕÍÑµ•¹ÑÌ°(€€€€€€€€€€€µ•Ñ…‘…Ñ„õÁ…å±½…¹µ•Ñ…‘…Ñ„°(€€€€€€€€¤(€€€€€€€¥˜¹½Ð½¬è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°ô‰…¥±•Ñ¼Í…Ù”…‘©ÕÍÑµ•¹ÑÌˆ¤((€€€€€€€É•½É€ô‘ˆ¹•Ñ}…±¥‰É…Ñ¥½¹}…‘©ÕÍÑµ•¹ÑÌ¡Í±Õœ¤½Èíô(€€€€€€€…‘©ÕÍÑµ•¹ÑÌ€ô}Í…™•}©Í½¹}‘¥Ð¡É•½É¹•Ð ‰…‘©ÕÍÑµ•¹ÑÌˆ¤¤(€€€€€€€µ•Ñ…‘…Ñ„€ô}Í…™•}©Í½¹}‘¥Ð¡É•½É¹•Ð ‰µ•Ñ…‘…Ñ„ˆ¤¤((€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰½¬ˆ°(€€€€€€€€€€€€‰…‘©ÕÍÑµ•¹ÑÌˆè…‘©ÕÍÑµ•¹ÑÌ°(€€€€€€€€€€€€‰µ•Ñ…‘…Ñ„ˆèµ•Ñ…‘…Ñ„°(€€€€€€€ô(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹Á½ÍÐ ˆ½‘ÉÕµµ•ÉÌ½íÍ±Õô½•¹•É…Ñ”ˆ¤)…Íå¹Œ‘•˜ÑÉ¥•É}•¹•É…Ñ¥½¸ (€€€Í±ÕœèÍÑÈ°(€€€‰…­É½Õ¹‘}Ñ…Í­Ìè	…­É½Õ¹‘Q…Í­Ì°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø¥ÑmÍÑÈ°¹åtè(€€€Í±Õœ€ô€¡Í±Õœ½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÍ±Õœè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œ‘ÉÕµµ•ÈÍ±Õœˆ¤((€€€ÑÉäè(€€€€€€€ÉÕ¹}¥‘}Í••€ôÍÑÈ¡ÕÕ¥¹ÕÕ¥Ð ¤¤(€€€€€€€ÅÕ•Õ•}µ•Ñ…‘…Ñ„€ôì(€€€€€€€€€€€€‰ÅÕ•Õ•ˆèQÉÕ”°(€€€€€€€€€€€€‰ÅÕ•Õ•‘}…Ðˆè‘…Ñ•Ñ¥µ”¹ÕÑ¹½Ü ¤¹¥Í½™½Éµ…Ð ¤°(€€€€€€€ô((€€€€€€€ÉÕ¹}¥€ô‘ˆ¹±½}…±¥‰É…Ñ¥½¹}ÉÕ¸ (€€€€€€€€€€€ÉÕ¹}¥õÉÕ¹}¥‘}Í••°(€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€½ÕÑ½µ”ô‰Á•¹‘¥¹œˆ°(€€€€€€€€€€€µ•Ñ…‘…Ñ„õÅÕ•Õ•}µ•Ñ…‘…Ñ„°(€€€€€€€€¤(€€€€€€€¥˜¹½ÐÉÕ¹}¥è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°ô‰…¥±•Ñ¼ÅÕ•Õ”…±¥‰É…Ñ¥½¸ÉÕ¸ˆ¤((€€€€€€€‰…­É½Õ¹‘}Ñ…Í­Ì¹…‘‘}Ñ…Í¬¡}½µÁ±•Ñ•}•¹•É…Ñ¥½¹}ÉÕ¸°Í±ÕœõÍ±Õœ°ÉÕ¹}¥õÉÕ¹}¥¤((€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰ÅÕ•Õ•ˆ°(€€€€€€€€€€€€‰ÉÕ¹}¥ˆèÉÕ¹}¥°(€€€€€€€€€€€€‰É½±±ÕÁM…Ù•ˆè…±Í”°(€€€€€€€ô(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤(()É½ÕÑ•È¹Á½ÍÐ ˆ½™••‘‰…¬ˆ¤)…Íå¹Œ‘•˜ÍÕ‰µ¥Ñ}™••‘‰…¬ (€€€Á…å±½…è••‘‰…­MÕ‰µ¥ÑI•ÅÕ•ÍÐ°(€€€‘ˆè•¹ÑÉ…±…Ñ…‰…Í•M•ÉÙ¥”€ô•Á•¹‘Ì¡•Ñ}‘‰}Í•ÉÙ¥”¤°(¤€´ø¥ÑmÍÑÈ°¹åtè(€€€Í±Õœ€ô€¡Á…å±½…¹‘ÉÕµµ•È½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÍ±Õœè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰5¥ÍÍ¥¹œ‘ÉÕµµ•ÈÍ±Õœˆ¤((€€€½µµ•¹Ð€ô€¡Á…å±½…¹½µµ•¹Ð½È€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð½µµ•¹Ðè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÐÀÁ}	}IEUMP°‘•Ñ…¥°ô‰••‘‰…¬½µµ•¹ÐÉ•ÅÕ¥É•ˆ¤((€€€ÑÉäè(€€€€€€€µ•Ñ…‘…Ñ„è¥ÑmÍÑÈ°¹åt€ôíô(€€€€€€€¥˜Á…å±½…¹ÉÕ¹}¥è(€€€€€€€€€€€µ•Ñ…‘…Ñ…l‰ÉÕ¹}¥‰t€ôÁ…å±½…¹ÉÕ¹}¥¹ÍÑÉ¥À ¤(€€€€€€€¥˜Á…å±½…¹¥Ñ•µ}¥è(€€€€€€€€€€€µ•Ñ…‘…Ñ…l‰¥Ñ•µ}¥‰t€ôÁ…å±½…¹¥Ñ•µ}¥¹ÍÑÉ¥À ¤((€€€€€€€™••‘‰…­}¥€ô‘ˆ¹±½}…±¥‰É…Ñ¥½¹}™••‘‰…¬ (€€€€€€€€€€€‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°(€€€€€€€€€€€É…Ñ¥¹œõÁ…å±½…¹É…Ñ¥¹œ°(€€€€€€€€€€€½µµ•¹Ðõ½µµ•¹Ð°(€€€€€€€€€€€…ÕÑ¡½Èô¡Á…å±½…¹…ÕÑ¡½È½È€‰Õ•ÍÐˆ¤¹ÍÑÉ¥À ¤½È€‰Õ•ÍÐˆ°(€€€€€€€€€€€µ•Ñ…‘…Ñ„õµ•Ñ…‘…Ñ„¥˜µ•Ñ…‘…Ñ„•±Í”9½¹”°(€€€€€€€€¤((€€€€€€€¥˜¹½Ð™••‘‰…­}¥è(€€€€€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°ô‰…¥±•Ñ¼É•½É™••‘‰…¬ˆ¤((€€€€€€€±…Ñ•ÍÐ€ô‘ˆ¹•Ñ}…±¥‰É…Ñ¥½¹}™••‘‰…¬¡‘ÉÕµµ•É}Í±ÕœõÍ±Õœ°±¥µ¥ÐôÈÔ¤(€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€‰ÍÑ…ÑÕÌˆè€‰½¬ˆ°(€€€€€€€€€€€€‰™••‘‰…­}¥ˆè™••‘‰…­}¥°(€€€€€€€€€€€€‰™••‘‰…¬ˆèm}Í•É¥…±¥é•}™••‘‰…¬¡¥Ñ•´¤™½È¥Ñ•´¥¸±…Ñ•ÍÑt°(€€€€€€€ô(€€€•á•ÁÐ!QQAá•ÁÑ¥½¸è(€€€€€€€É…¥Í”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì•áŒè(€€€€€€€É…¥Í”!QQAá•ÁÑ¥½¸¡ÍÑ…ÑÕÍ}½‘”õÍÑ…ÑÕÌ¹!QQA|ÔÀÁ}%9QI91}MIYI}II=H°‘•Ñ…¥°õÍÑÈ¡•áŒ¤¤()…ÁÀ¹¥¹±Õ‘•}É½ÕÑ•È¡É½ÕÑ•È¤)…ÁÀ¹¥¹±Õ‘•}É½ÕÑ•È¡…±¥‰É…Ñ¥½¹}ØÉ}É½ÕÑ•È¤)…ÁÀ¹¥¹±Õ‘•}É½ÕÑ•È¡…ÍÍ¥µ¥±…Ñ¥½¹}•¹•É…Ñ¥½¹}É½ÕÑ•È¤)…ÁÀ¹¥¹±Õ‘•}É½ÕÑ•È¡ÍÑÕ‘¥½µ¥¹‘}ÑÉ…­…¥}É½ÕÑ•È¤)…ÁÀ¹¥¹±Õ‘•}É½ÕÑ•È¡ÍÑÕ‘¥½µ¥¹‘}ÑÉ…­…¥}•¹•É…Ñ¥½¹}É½ÕÑ•È¤(
"""Authenticated reviewer and administrator routes for Calibration v2.

Reviewer responses intentionally omit the hidden A/B treatment mapping, profile
snapshots, model internals, database diagnostics, and worker controls.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Literal, Optional
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from admin.services.central_database_service import CentralDatabaseService
from backend.security.supabase_auth import AuthenticatedUser, require_authenticated_user
from backend.services.artifact_url_service import ArtifactUrlService
from backend.services.calibration_assimilation_service import (
    CalibrationAssimilationService,
    CalibrationProvisioningUnavailable,
)
from backend.services.calibration_profile_resolver import CalibrationProfileUnavailable, validate_profile_overrides
from backend.services.calibration_production_engine import validate_cfg_overrides
from backend.services.calibration_trial_readiness import CalibrationTrialReadinessService
from backend.services.calibration_trial_service import CalibrationDependencyError, CalibrationTrialService, TrialCreateInput
from backend.services.production_performance_client import ProductionGenerationUnavailable
from backend.trackai_platform import available_instrument_specs
from backend.services.calibration_v2_repository import (
    CalibrationAuthorizationError,
    CalibrationRecordNotFound,
    CalibrationV2Repository,
)


from backend.trackai_platform.bass_assimilation import BassAssimilationService
from backend.trackai_platform.bass_contracts import BassSourceObservation
from backend.trackai_platform.source_intake import InMemorySourceEvidenceRepository

router = APIRouter(prefix="/calibration/v2", tags=["calibration-v2"])
logger = logging.getLogger(__name__)
_artifact_urls = ArtifactUrlService()
_bass_source_repository = InMemorySourceEvidenceRepository()
_bass_assimilation = BassAssimilationService(_bass_source_repository)



def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _require_v2_enabled() -> None:
    if not _env_bool("CALIBRATION_V2_ENABLED", default=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Calibration v2 is disabled",
        )


def _require_external_reviewers_enabled() -> None:
    _require_v2_enabled()
    if not _env_bool("CALIBRATION_EXTERNAL_REVIEWERS_ENABLED", default=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External calibration review is not open",
        )


def _db_service() -> CentralDatabaseService:
    # Lazy import avoids a cycle when backend.calibration_api includes this router.
    from backend.calibration_api import get_db_service

    return get_db_service()


def _repository(db: CentralDatabaseService = Depends(_db_service)) -> CalibrationV2Repository:
    return CalibrationV2Repository(db)


def _readiness_service(
    db: CentralDatabaseService = Depends(_db_service),
) -> CalibrationTrialReadinessService:
    return CalibrationTrialReadinessService(db)


def _assimilation_service(
    db: CentralDatabaseService = Depends(_db_service),
) -> CalibrationAssimilationService:
    return CalibrationAssimilationService(db)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CalibrationAuthorizationError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, CalibrationRecordNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, CalibrationProvisioningUnavailable):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CalibrationDependencyError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if isinstance(exc, ProductionGenerationUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Production generation is unavailable")
    if isinstance(exc, CalibrationProfileUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Calibration profile is unavailable")
    logger.exception("calibration_v2_request_failed")
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Calibration request failed. The error was logged with the server request ID.",
    )


def _require_admin(
    user: AuthenticatedUser = Depends(require_authenticated_user),
    repo: CalibrationV2Repository = Depends(_repository),
) -> AuthenticatedUser:
    _require_v2_enabled()
    try:
        repo.require_role(user.user_id, {"admin", "research_admin"})
        return user
    except Exception as exc:
        raise _http_error(exc) from exc


def _require_reviewer_context(
    user: AuthenticatedUser = Depends(require_authenticated_user),
    repo: CalibrationV2Repository = Depends(_repository),
):
    """Authorize public reviewers or explicitly enabled internal pilot reviewers.

    ``ALLOW_UNVERIFIED_JWT`` is deliberately not consulted here. Authentication
    must already have succeeded through ``require_authenticated_user``.
    """

    _require_v2_enabled()
    try:
        reviewer = repo.require_reviewer(user.user_id)

        if _env_bool("CALIBRATION_EXTERNAL_REVIEWERS_ENABLED", default=False):
            return reviewer

        if _env_bool("CALIBRATION_INTERNAL_REVIEWERS_ENABLED", default=False):
            roles = repo.roles_for_user(user.user_id)
            if roles.intersection({"admin", "research_admin", "internal_reviewer"}):
                return reviewer

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="External calibration review is not open",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc




class BassSourceIntakeRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    performer_profile_id: str = Field(min_length=1, max_length=200)
    provenance_uri: str = Field(min_length=1, max_length=2000)
    source_title: str = Field(min_length=1, max_length=500)
    tempo_bpm: float = Field(gt=20, lt=400)
    meter: str = Field(min_length=1, max_length=32)
    key_center: Optional[str] = None
    chord_map_id: Optional[str] = None
    kick_alignment_score: float = Field(ge=0, le=1)
    note_length_profile: str = Field(min_length=1, max_length=120)
    articulation_tags: List[str] = Field(default_factory=list)
    technique_tags: List[str] = Field(default_factory=list)
    extraction_version: str = Field(min_length=1, max_length=120)
    human_reviewed: bool = False


class ReviewerProvisionRequest(BaseModel):
    reviewer_id: str = Field(min_length=3, max_length=100)
    auth_user_id: str
    display_name: str = Field(min_length=1, max_length=200)
    expertise_level: Optional[str] = None
    primary_styles: List[str] = Field(default_factory=list)
    years_experience: Optional[int] = Field(default=None, ge=0, le=80)
    weighting_factor: float = Field(default=1.0, ge=0.25, le=4.0)
    consent_version: str = "calibration_reviewer_consent_v1"
    is_active: bool = True


class TreatmentCreateRequest(BaseModel):
    drummer_slug: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    base_model_version: Optional[str] = None
    cfg_overrides: Dict[str, Any] = Field(default_factory=dict)
    profile_overrides: Dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "active"] = "draft"


class TrialCreateRequest(BaseModel):
    reviewer_id: str
    drummer_slug: str
    base_groove_id: str = "base_groove"
    challenger_treatment_id: str
    paired_seed: int = Field(default_factory=lambda: int(uuid.uuid4().int % 2_000_000_000))
    assignment_seed: int = Field(default_factory=lambda: int(uuid.uuid4().int % 2_000_000_000))
    repeats: int = Field(default=4, ge=1, le=16)
    render_profile_id: str = "calibration_standard_v2"
    sample_pack_version: str = "default"
    kit_id: str = "default_kit"


Choice = Literal["A", "B", "tie", "neither"]


class JudgmentSubmitRequest(BaseModel):
    preferred_candidate: Choice
    closer_to_target: Choice
    better_feel: Choice
    more_musical: Choice
    confidence: int = Field(ge=1, le=5)
    technical_issue: bool = False
    cannot_judge: bool = False
    comment: Optional[str] = Field(default=None, max_length=4000)
    listening_ms: int = Field(default=0, ge=0, le=7_200_000)
    candidate_a_listening_ms: int = Field(default=0, ge=0, le=3_600_000)
    candidate_b_listening_ms: int = Field(default=0, ge=0, le=3_600_000)
    candidate_a_play_count: int = Field(default=0, ge=0, le=1000)
    candidate_b_play_count: int = Field(default=0, ge=0, le=1000)


class Ratings(BaseModel):
    stylistic_authenticity: int = Field(ge=1, le=10)
    groove_feel: int = Field(ge=1, le=10)
    dynamics: int = Field(ge=1, le=10)
    phrasing: int = Field(ge=1, le=10)
    kit_balance: int = Field(ge=1, le=10)
    fill_behavior: int = Field(ge=1, le=10)
    human_realism: int = Field(ge=1, le=10)
    overall_usefulness: int = Field(ge=1, le=10)


class RatingSubmitRequest(BaseModel):
    candidate_label: Literal["A", "B"]
    ratings: Ratings


class ReviewSubmitRequest(JudgmentSubmitRequest):
    ratings_a: Optional[Ratings] = None
    ratings_b: Optional[Ratings] = None


def _serialize_artifact(artifact: Any) -> Optional[Dict[str, Any]]:
    storage_uri = str(getattr(artifact, "storage_uri", "") or "").strip()
    url = _artifact_urls.build_url(storage_uri)
    if not url:
        return None
    return {
        "artifact_id": str(getattr(artifact, "artifact_id", "") or ""),
        "artifact_type": str(
            getattr(artifact, "artifact_type", "candidate_audio")
            or "candidate_audio"
        ),
        "url": url,
        "duration_sec": getattr(artifact, "duration_sec", None),
        "loudness_lufs": getattr(artifact, "loudness_lufs", None),
        "sample_pack_version": getattr(artifact, "sample_pack_version", None),
    }


def _lane_artifacts(db: CentralDatabaseService, run_id: Optional[str]) -> List[Dict[str, Any]]:
    if not run_id:
        return []
    output = []
    for artifact in db.get_audio_artifacts_for_run(run_id=run_id) or []:
        serialized = _serialize_artifact(artifact)
        if serialized:
            output.append(serialized)
    return output


def _reviewer_item_payload(
    *,
    row: Dict[str, Any],
    repo: CalibrationV2Repository,
    db: CentralDatabaseService,
) -> Dict[str, Any]:
    # Never include ab_mapping_json or calibration_trials.assignment_json here.
    return {
        "item_id": str(row["item_id"]),
        "session_id": str(row["session_id"]),
        "trial_id": str(row["trial_id"]),
        "base_groove_id": str(row["base_groove_id"]),
        "target_drummer_slug": str(row["target_drummer_slug"]),
        "target_drummer_display_name": repo.drummer_display_name(str(row["target_drummer_slug"])),
        "eval_mode": "AB",
        "lanes": {
            "neutral": _lane_artifacts(db, row.get("baseline_run_id")),
            "A": _lane_artifacts(db, row.get("candidate_a_run_id")),
            "B": _lane_artifacts(db, row.get("candidate_b_run_id")),
        },
        "rubric": {
            "choices": ["A", "B", "tie", "neither"],
            "rating_min": 1,
            "rating_max": 10,
            "minimum_listening_seconds_per_candidate": int(
                os.getenv("CALIBRATION_MIN_LISTENING_SECONDS", "10")
            ),
        },
    }


@router.get("/healthz")
def v2_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "version": "calibration_v2",
        "enabled": _env_bool("CALIBRATION_V2_ENABLED", default=False),
        "internal_reviewers_enabled": _env_bool(
            "CALIBRATION_INTERNAL_REVIEWERS_ENABLED", default=False
        ),
        "external_reviewers_enabled": _env_bool(
            "CALIBRATION_EXTERNAL_REVIEWERS_ENABLED", default=False
        ),
        "auto_queue_review_trials": _env_bool(
            "CALIBRATION_AUTO_QUEUE_REVIEW_TRIALS", default=False
        ),
    }


@router.get("/platform/instruments")
def platform_instruments(context=Depends(_require_reviewer_context)) -> Dict[str, Any]:
    """Return the shared TracKAI instrument registry without granting generation authority."""
    del context
    return {
        "schema_version": "1.0.0",
        "items": [
            {
                "instrument_id": spec.instrument_id,
                "product_id": spec.product_id,
                "display_name": spec.display_name,
                "subject_label": spec.subject_label,
                "source_entity_label": spec.source_entity_label,
                "generation_role": spec.generation_role,
                "conditioning_inputs": list(spec.conditioning_inputs),
                "ratings": [
                    {"key": r.key, "label": r.label, "description": r.description}
                    for r in spec.ratings
                ],
                "calibration_available": spec.instrument_id == "drums",
                "execution_authorized": False,
            }
            for spec in available_instrument_specs()
        ],
        "execution_authorized": False,
    }


@router.get("/reviewer/me")
def reviewer_me(context=Depends(_require_reviewer_context)) -> Dict[str, Any]:
    return {
        "reviewer_id": context.reviewer_id,
        "display_name": context.display_name,
        "expertise_level": context.expertise_level,
        "consent_version": context.consent_version,
        "is_active": context.is_active,
    }


@router.get("/reviewer/drummers")
def reviewer_drummers(
    context=Depends(_require_reviewer_context),
    readiness: CalibrationTrialReadinessService = Depends(_readiness_service),
    assimilation: CalibrationAssimilationService = Depends(_assimilation_service),
) -> Dict[str, Any]:
    try:
        readiness.refresh_for_reviewer(context.reviewer_id)
        models = assimilation.list_models(
            reviewer_id=context.reviewer_id,
            include_blocked=False,
        )
        return {"items": [model.reviewer_payload() for model in models]}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/reviewer/next")
def reviewer_next(
    target_drummer_slug: Optional[str] = Query(default=None),
    context=Depends(_require_reviewer_context),
    repo: CalibrationV2Repository = Depends(_repository),
    readiness: CalibrationTrialReadinessService = Depends(_readiness_service),
    assimilation: CalibrationAssimilationService = Depends(_assimilation_service),
    db: CentralDatabaseService = Depends(_db_service),
) -> Dict[str, Any]:
    try:
        slug = str(target_drummer_slug or "").strip()
        readiness.refresh_for_reviewer(context.reviewer_id)
        row = repo.next_ready_item_for_reviewer(
            reviewer_id=context.reviewer_id,
            target_drummer_slug=slug or None,
        )
        if row:
            repo.mark_session_started(str(row["session_id"]))
            return {
                "item": _reviewer_item_payload(row=row, repo=repo, db=db),
                "status": "ready",
                "trial_id": str(row.get("trial_id") or ""),
                "retry_after_seconds": 0,
            }

        if slug and _env_bool("CALIBRATION_AUTO_QUEUE_REVIEW_TRIALS", default=False):
            queued = assimilation.ensure_reviewer_trial(
                reviewer_id=context.reviewer_id,
                drummer_slug=slug,
            )
            queued_status = str(queued.get("status") or "queued").lower()
            return {
                "item": None,
                "status": "preparing" if queued_status == "queued" else queued_status,
                "trial_id": str(queued.get("trial_id") or ""),
                "message": "The assimilated drummer comparison is being generated and rendered.",
                "retry_after_seconds": max(
                    2,
                    int(os.getenv("CALIBRATION_REVIEW_POLL_SECONDS", "5")),
                ),
            }

        return {
            "item": None,
            "status": "none",
            "trial_id": None,
            "message": "No rendered comparison is currently assigned.",
            "retry_after_seconds": 0,
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/reviewer/items/{item_id}")
def reviewer_item(
    item_id: str,
    context=Depends(_require_reviewer_context),
    repo: CalibrationV2Repository = Depends(_repository),
    readiness: CalibrationTrialReadinessService = Depends(_readiness_service),
    db: CentralDatabaseService = Depends(_db_service),
) -> Dict[str, Any]:
    try:
        readiness.refresh_for_reviewer(context.reviewer_id)
        row = repo.require_item_ownership(item_id=item_id, reviewer_id=context.reviewer_id)
        if str(row.get("trial_status") or "").strip().lower() != "ready":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Calibration item is not ready for reviewer playback",
            )
        repo.mark_session_started(str(row["session_id"]))
        return {"item": _reviewer_item_payload(row=row, repo=repo, db=db)}
    except HTTPException:
        raise
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/reviewer/items/{item_id}/review")
def submit_review(
    item_id: str,
    payload: ReviewSubmitRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    context=Depends(_require_reviewer_context),
    repo: CalibrationV2Repository = Depends(_repository),
) -> Dict[str, Any]:
    key = str(idempotency_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")

    if not payload.cannot_judge and not payload.technical_issue:
        minimum_ms = int(os.getenv("CALIBRATION_MIN_LISTENING_SECONDS", "10")) * 1000
        if payload.candidate_a_play_count < 1 or payload.candidate_b_play_count < 1:
            raise HTTPException(status_code=422, detail="Both candidates must be played before submission")
        if payload.candidate_a_listening_ms < minimum_ms or payload.candidate_b_listening_ms < minimum_ms:
            raise HTTPException(status_code=422, detail="Minimum listening time has not been met for both candidates")
        if payload.ratings_a is None or payload.ratings_b is None:
            raise HTTPException(status_code=422, detail="Complete A and B ratings are required")

    ratings_by_label: Dict[str, Dict[str, int]] = {}
    if payload.ratings_a is not None:
        ratings_by_label["A"] = payload.ratings_a.model_dump()
    if payload.ratings_b is not None:
        ratings_by_label["B"] = payload.ratings_b.model_dump()

    try:
        result = repo.submit_review(
            item_id=item_id,
            reviewer_id=context.reviewer_id,
            judgment={
                "preferred_candidate": payload.preferred_candidate,
                "closer_to_target": payload.closer_to_target,
                "better_feel": payload.better_feel,
                "more_musical": payload.more_musical,
                "confidence": payload.confidence,
                "technical_issue": payload.technical_issue,
                "cannot_judge": payload.cannot_judge,
                "comment": payload.comment,
                "listening_ms": payload.listening_ms,
                "candidate_a_listening_ms": payload.candidate_a_listening_ms,
                "candidate_b_listening_ms": payload.candidate_b_listening_ms,
                "candidate_a_play_count": payload.candidate_a_play_count,
                "candidate_b_play_count": payload.candidate_b_play_count,
            },
            ratings_by_label=ratings_by_label,
            idempotency_key=key,
        )
        return {"status": "ok", **result}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/admin/reviewers")
def provision_reviewer(
    payload: ReviewerProvisionRequest,
    _admin: AuthenticatedUser = Depends(_require_admin),
    repo: CalibrationV2Repository = Depends(_repository),
) -> Dict[str, Any]:
    try:
        reviewer_id = repo.upsert_reviewer(**payload.model_dump())
        return {"status": "ok", "reviewer_id": reviewer_id}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/admin/treatments")
def create_treatment(
    payload: TreatmentCreateRequest,
    admin: AuthenticatedUser = Depends(_require_admin),
    repo: CalibrationV2Repository = Depends(_repository),
) -> Dict[str, Any]:
    try:
        cfg_overrides = validate_cfg_overrides(payload.cfg_overrides)
        profile_overrides = validate_profile_overrides(payload.profile_overrides)
        treatment_id = repo.create_treatment(
            drummer_slug=payload.drummer_slug,
            name=payload.name,
            description=payload.description,
            cfg_overrides=cfg_overrides,
            profile_overrides=profile_overrides,
            base_model_version=payload.base_model_version,
            created_by=admin.user_id,
            status_value=payload.status,
        )
        return {"status": "ok", "treatment_id": treatment_id}
    except Exception as exc:
        raise _http_error(exc) from exc




@router.post("/admin/platform/bass/sources")
def ingest_bass_source(
    payload: BassSourceIntakeRequest,
    _admin: AuthenticatedUser = Depends(_require_admin),
) -> Dict[str, Any]:
    try:
        observation = BassSourceObservation(
            source_id=payload.source_id, performer_profile_id=payload.performer_profile_id,
            provenance_uri=payload.provenance_uri, tempo_bpm=payload.tempo_bpm, meter=payload.meter,
            key_center=payload.key_center, chord_map_id=payload.chord_map_id,
            kick_alignment_score=payload.kick_alignment_score, note_length_profile=payload.note_length_profile,
            articulation_tags=tuple(payload.articulation_tags), technique_tags=tuple(payload.technique_tags),
            extraction_version=payload.extraction_version,
        )
        fingerprint = _bass_assimilation.ingest_observation(
            observation, source_title=payload.source_title, human_reviewed=payload.human_reviewed
        )
        return {"status":"ok","evidence_fingerprint":fingerprint,"execution_authorized":False}
    except Exception as exc:
        raise _http_error(exc) from exc

@router.get("/admin/platform/bass/datasets/{performer_profile_id}")
def bass_dataset_status(
    performer_profile_id: str,
    _admin: AuthenticatedUser = Depends(_require_admin),
) -> Dict[str, Any]:
    status_value = _bass_assimilation.status(performer_profile_id)
    return {"status":"ok","dataset":status_value.__dict__}

@router.post("/admin/platform/bass/datasets/{performer_profile_id}/build-profile")
def build_bass_assimilation_profile(
    performer_profile_id: str,
    _admin: AuthenticatedUser = Depends(_require_admin),
) -> Dict[str, Any]:
    try:
        profile = _bass_assimilation.build_profile(performer_profile_id)
        return {"status":"ok","profile":profile.__dict__,"execution_authorized":False}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/admin/drummers/assimilation")
def list_assimilation_models(
    include_blocked: bool = Query(default=True),
    _admin: AuthenticatedUser = Depends(_require_admin),
    assimilation: CalibrationAssimilationService = Depends(_assimilation_service),
) -> Dict[str, Any]:
    try:
        models = assimilation.list_models(include_blocked=include_blocked)
        return {"status": "ok", "items": [model.admin_payload() for model in models]}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/admin/drummers/{drummer_slug}/assimilation")
def assimilation_model_status(
    drummer_slug: str,
    _admin: AuthenticatedUser = Depends(_require_admin),
    assimilation: CalibrationAssimilationService = Depends(_assimilation_service),
) -> Dict[str, Any]:
    try:
        return {
            "status": "ok",
            "model": assimilation.model_status(drummer_slug).admin_payload(),
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/admin/drummers/{drummer_slug}/bootstrap-treatment")
def bootstrap_assimilation_treatment(
    drummer_slug: str,
    admin: AuthenticatedUser = Depends(_require_admin),
    assimilation: CalibrationAssimilationService = Depends(_assimilation_service),
) -> Dict[str, Any]:
    try:
        result = assimilation.bootstrap_phase6_treatment(
            drummer_slug=drummer_slug,
            created_by=admin.user_id,
        )
        treatment = result.get("treatment") if isinstance(result.get("treatment"), dict) else {}
        return {
            "status": "ok",
            "created": bool(result.get("created")),
            "treatment_id": treatment.get("treatment_id"),
            "drummer_slug": drummer_slug,
            "cfg_overrides": treatment.get("cfg_overrides") or {},
        }
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/admin/trials")
def create_trial(
    payload: TrialCreateRequest,
    _admin: AuthenticatedUser = Depends(_require_admin),
    db: CentralDatabaseService = Depends(_db_service),
) -> Dict[str, Any]:
    try:
        service = CalibrationTrialService(db)
        return service.create_trial(TrialCreateInput(**payload.model_dump()))
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/admin/trials/{trial_id}/readiness")
def trial_readiness_diagnostics(
    trial_id: str,
    _admin: AuthenticatedUser = Depends(_require_admin),
    readiness: CalibrationTrialReadinessService = Depends(_readiness_service),
) -> Dict[str, Any]:
    try:
        diagnostics = readiness.diagnostics_for_trial(trial_id)
        if not diagnostics:
            raise CalibrationRecordNotFound(f"Trial not found: {trial_id}")
        return {"status": "ok", "readiness": diagnostics}
    except Exception as exc:
        raise _http_error(exc) from exc

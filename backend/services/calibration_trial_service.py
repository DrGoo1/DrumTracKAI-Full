"""Create controlled, blinded calibration trials around the production engine."""
from __future__ import annotations

from dataclasses import dataclass
import os
import random
from typing import Any, Dict, Optional
import uuid

from admin.services.central_database_service import CentralDatabaseService
from backend.services.calibration_production_engine import (
    CalibrationProductionEngine,
    ProductionCandidate,
    validate_treatment_overrides,
)
from backend.services.calibration_render_service import CalibrationRenderService, RenderRequest
from backend.services.calibration_v2_repository import CalibrationV2Repository
from backend.services.production_performance_client import ProductionGenerationUnavailable


@dataclass(frozen=True)
class TrialCreateInput:
    reviewer_id: str
    drummer_slug: str
    base_groove_id: str
    challenger_treatment_id: str
    paired_seed: int
    assignment_seed: int
    repeats: int = 4
    render_profile_id: str = "calibration_standard_v2"
    sample_pack_version: str = "default"
    kit_id: str = "default_kit"


def assign_visible_roles(assignment_seed: int) -> tuple[str, str]:
    lane_pairs = ["control", "challenger"]
    random.Random(int(assignment_seed)).shuffle(lane_pairs)
    return lane_pairs[0], lane_pairs[1]


class CalibrationDependencyError(RuntimeError):
    """Raised when required external generation infrastructure is unavailable."""


class CalibrationTrialService:
    def __init__(
        self,
        db: CentralDatabaseService,
        *,
        repository: Optional[CalibrationV2Repository] = None,
        engine: Optional[CalibrationProductionEngine] = None,
        render_service: Optional[CalibrationRenderService] = None,
    ) -> None:
        self._db = db
        self._repo = repository or CalibrationV2Repository(db)
        self._engine = engine or CalibrationProductionEngine(db)
        self._render = render_service or CalibrationRenderService(db)

    def _app_env(self) -> str:
        return str(os.getenv("APP_ENV", "development")).strip().lower()

    def _fallback_allowed(self) -> bool:
        mode = str(os.getenv("CALIBRATION_GENERATION_MODE", "")).strip().lower()
        return self._app_env() == "development" and mode == "inprocess"

    def _require_canonical_backend(self, *, candidate: ProductionCandidate, role: str) -> None:
        metadata = candidate.metadata.get("production_metadata") if isinstance(candidate.metadata, dict) else {}
        backend_name = str((metadata or {}).get("backend") or "").strip().lower()
        engine_mode = str(candidate.metadata.get("production_engine_mode") or "").strip().lower()
        if self._app_env() in {"production", "staging"}:
            if engine_mode != "http":
                raise CalibrationDependencyError(
                    f"{role} generation is not using canonical HTTP generation mode (got '{engine_mode or 'unknown'}')"
                )
            if backend_name == "fallback":
                raise CalibrationDependencyError(
                    f"{role} generation reported fallback backend in {self._app_env()} mode"
                )
        elif backend_name == "fallback" and not self._fallback_allowed():
            raise CalibrationDependencyError(
                "Fallback backend is only allowed in explicit local-development mode "
                "(APP_ENV=development with CALIBRATION_GENERATION_MODE=inprocess)"
            )

    def _persist_run(self, *, trial_id: str, role: str, candidate: ProductionCandidate, request: TrialCreateInput) -> str:
        run_id = f"{trial_id}_{role}"
        run_meta = {
            "trial_id": trial_id,
            "role": role,
            "engine": candidate.metadata.get("engine"),
            "candidate_metadata": candidate.metadata,
            "tempo_bpm": candidate.tempo_bpm,
            "time_signature": candidate.time_signature,
            "bars": candidate.bars,
            "kit_id": request.kit_id,
            "base_groove_id": request.base_groove_id,
            "sample_pack_version": request.sample_pack_version,
            "render_profile_id": request.render_profile_id,
        }
        self._db.log_calibration_run(
            run_id=run_id,
            drummer_slug=request.drummer_slug,
            outcome="queued",
            metadata=run_meta,
        )
        self._render.render_run(
            RenderRequest(
                run_id=run_id,
                render_profile_id=request.render_profile_id,
                sample_pack_version=request.sample_pack_version,
                kit_id=request.kit_id,
                seed=int(request.paired_seed),
                render_recipe={
                    "schema": "calibration_render_recipe_v2",
                    "trial_id": trial_id,
                    "role": role,
                    "base_groove_id": request.base_groove_id,
                },
            )
        )
        return run_id

    def create_trial(self, request: TrialCreateInput) -> Dict[str, Any]:
        treatment = self._repo.get_treatment(request.challenger_treatment_id)
        if treatment["drummer_slug"] != request.drummer_slug:
            raise ValueError("Drummer slug mismatch between request and treatment")
        if treatment["status"] not in {"draft", "active"}:
            raise ValueError("Treatment must be draft or active")
        normalized_treatment = validate_treatment_overrides(
            cfg_overrides=treatment.get("cfg_overrides") or {},
            profile_overrides=treatment.get("profile_overrides") or {},
        )

        try:
            neutral = self._engine.generate_neutral(
                base_groove_id=request.base_groove_id,
                repeats=request.repeats,
                seed=request.paired_seed,
                kit_id=request.kit_id,
            )
            control = self._engine.generate_candidate(
                role="control",
                base_groove_id=request.base_groove_id,
                drummer_slug=request.drummer_slug,
                seed=request.paired_seed,
                repeats=request.repeats,
                cfg_overrides={},
                profile_overrides={},
                treatment_id=None,
                kit_id=request.kit_id,
            )
            challenger = self._engine.generate_candidate(
                role="challenger",
                base_groove_id=request.base_groove_id,
                drummer_slug=request.drummer_slug,
                seed=request.paired_seed,
                repeats=request.repeats,
                cfg_overrides=normalized_treatment["cfg_overrides"],
                profile_overrides=normalized_treatment["profile_overrides"],
                treatment_id=request.challenger_treatment_id,
                kit_id=request.kit_id,
            )
        except ProductionGenerationUnavailable as exc:
            raise CalibrationDependencyError(f"Production generation unavailable: {exc}") from exc

        self._require_canonical_backend(candidate=control, role="control")
        self._require_canonical_backend(candidate=challenger, role="challenger")

        if control.metadata["base_pattern_hash"] != challenger.metadata["base_pattern_hash"]:
            raise RuntimeError("Control/challenger base-pattern hashes differ")
        if control.metadata["paired_seed"] != challenger.metadata["paired_seed"]:
            raise RuntimeError("Control/challenger paired seeds differ")
        if control.metadata["event_stream_hash"] == challenger.metadata["event_stream_hash"]:
            raise RuntimeError(
                "Challenger produced no audible event change. Refuse to create a meaningless trial."
            )

        lane_a_role, lane_b_role = assign_visible_roles(request.assignment_seed)
        trial_id = f"trial_{uuid.uuid4().hex[:16]}"

        run_ids = {
            "neutral": self._persist_run(trial_id=trial_id, role="neutral", candidate=neutral, request=request),
            "control": self._persist_run(trial_id=trial_id, role="control", candidate=control, request=request),
            "challenger": self._persist_run(trial_id=trial_id, role="challenger", candidate=challenger, request=request),
        }

        visible_a_run_id = run_ids[lane_a_role]
        visible_b_run_id = run_ids[lane_b_role]

        session_id = self._db.create_evaluation_session(
            reviewer_id=request.reviewer_id,
            target_drummer_slug=request.drummer_slug,
            app_version="calibration_v2",
        )
        item_id = self._db.create_evaluation_item(
            session_id=str(session_id or ""),
            base_groove_id=request.base_groove_id,
            target_drummer_slug=request.drummer_slug,
            baseline_run_id=run_ids["neutral"],
            candidate_a_run_id=visible_a_run_id,
            candidate_b_run_id=visible_b_run_id,
            eval_mode="AB",
            ab_mapping={"A": lane_a_role, "B": lane_b_role},
        )

        model_version = str(
            challenger.metadata.get("production_metadata", {}).get("model_version")
            or os.getenv("DRUMTRACKAI_MODEL_VERSION", "unknown")
        )

        self._repo.create_trial_record(
            {
                "trial_id": trial_id,
                "item_id": str(item_id or f"item_{uuid.uuid4().hex[:12]}"),
                "session_id": str(session_id or f"sess_{uuid.uuid4().hex[:12]}"),
                "reviewer_id": request.reviewer_id,
                "drummer_slug": request.drummer_slug,
                "base_groove_id": request.base_groove_id,
                "neutral_run_id": run_ids["neutral"],
                "control_run_id": run_ids["control"],
                "challenger_run_id": run_ids["challenger"],
                "visible_a_run_id": visible_a_run_id,
                "visible_b_run_id": visible_b_run_id,
                "challenger_treatment_id": request.challenger_treatment_id,
                "paired_seed": int(request.paired_seed),
                "assignment_seed": int(request.assignment_seed),
                "control_profile_hash": str(control.metadata.get("profile_snapshot_hash") or ""),
                "challenger_profile_hash": str(challenger.metadata.get("profile_snapshot_hash") or ""),
                "control_profile_snapshot": control.profile_snapshot or {},
                "challenger_profile_snapshot": challenger.profile_snapshot or {},
                "assignment_json": {
                    "lane_a_role": lane_a_role,
                    "lane_b_role": lane_b_role,
                    "visible_a_run_id": visible_a_run_id,
                    "visible_b_run_id": visible_b_run_id,
                },
                "generation_metadata": {
                    "control": control.metadata,
                    "challenger": challenger.metadata,
                    "neutral": neutral.metadata,
                },
                "model_version": model_version,
                "renderer_version": str(os.getenv("CALIBRATION_RENDERER_VERSION", "unknown")),
                "sample_pack_version": request.sample_pack_version,
                "status": "queued",
            }
        )

        return {
            "status": "queued",
            "trial_id": trial_id,
            "item_id": item_id,
            "session_id": session_id,
            "run_ids": run_ids,
            "visible_roles": {"A": lane_a_role, "B": lane_b_role},
        }

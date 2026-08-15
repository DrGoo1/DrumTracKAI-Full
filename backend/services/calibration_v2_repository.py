"""Postgres-only persistence and authorization helpers for calibration v2."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set
import uuid

from sqlalchemy import text

from admin.services.central_database_service import CentralDatabaseService


class CalibrationAuthorizationError(PermissionError):
    pass


class CalibrationRecordNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ReviewerContext:
    reviewer_id: str
    auth_user_id: str
    display_name: str
    expertise_level: Optional[str]
    weighting_factor: float
    consent_version: Optional[str]
    consented_at: Optional[str]
    is_active: bool


class CalibrationV2Repository:
    def __init__(self, db: CentralDatabaseService) -> None:
        self._db = db
        self._engine = getattr(db, "_engine", None)
        if self._engine is None:
            raise RuntimeError("Calibration v2 requires an initialized Postgres engine")

    @staticmethod
    def _json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except Exception:
            return default

    def drummer_display_name(self, drummer_slug: str) -> str:
        record = self._db.get_drummer(str(drummer_slug or "").strip())
        if isinstance(record, dict):
            for key in ("display_name", "name", "drummer_name"):
                value = str(record.get(key) or "").strip()
                if value:
                    return value
        return " ".join(
            part.capitalize()
            for part in str(drummer_slug or "").replace("-", "_").split("_")
            if part
        )

    def list_ready_drummers(self, reviewer_id: str) -> List[Dict[str, Any]]:
        """Return only the target drummers with an unreviewed ready trial for this reviewer."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT t.drummer_slug, COUNT(*) AS ready_trial_count
                    FROM public.calibration_trials t
                    JOIN public.evaluation_sessions s ON s.session_id = t.session_id
                    WHERE s.reviewer_id = :reviewer_id
                      AND t.status = 'ready'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.pairwise_judgments j
                          WHERE j.item_id = t.item_id
                            AND j.reviewer_id = :reviewer_id
                      )
                    GROUP BY t.drummer_slug
                    ORDER BY t.drummer_slug
                    """
                ),
                {"reviewer_id": reviewer_id},
            ).mappings().all()
        return [
            {
                "drummer_slug": str(row["drummer_slug"]),
                "display_name": self.drummer_display_name(str(row["drummer_slug"])),
                "ready_trial_count": int(row.get("ready_trial_count") or 0),
            }
            for row in rows
        ]

    def roles_for_user(self, auth_user_id: str) -> Set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT role FROM public.app_user_roles WHERE user_id = CAST(:uid AS uuid)"),
                {"uid": auth_user_id},
            ).all()
        return {str(row[0]).strip().lower() for row in rows if row and str(row[0]).strip()}

    def require_role(self, auth_user_id: str, allowed: Sequence[str]) -> Set[str]:
        allowed_set = {str(role).strip().lower() for role in allowed}
        roles = self.roles_for_user(auth_user_id)
        if not roles.intersection(allowed_set):
            raise CalibrationAuthorizationError(
                f"Required role not present. Required one of {sorted(allowed_set)}"
            )
        return roles

    def reviewer_for_auth_user(self, auth_user_id: str) -> Optional[ReviewerContext]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT reviewer_id, CAST(auth_user_id AS text) AS auth_user_id,
                           display_name, expertise_level, weighting_factor,
                           consent_version, CAST(consented_at AS text) AS consented_at,
                           is_active
                    FROM public.reviewer_profiles
                    WHERE auth_user_id = CAST(:uid AS uuid)
                    LIMIT 1
                    """
                ),
                {"uid": auth_user_id},
            ).mappings().first()
        if not row:
            return None
        return ReviewerContext(
            reviewer_id=str(row["reviewer_id"]),
            auth_user_id=str(row["auth_user_id"]),
            display_name=str(row["display_name"]),
            expertise_level=(str(row["expertise_level"]) if row.get("expertise_level") else None),
            weighting_factor=float(row.get("weighting_factor") or 1.0),
            consent_version=(str(row["consent_version"]) if row.get("consent_version") else None),
            consented_at=(str(row["consented_at"]) if row.get("consented_at") else None),
            is_active=bool(row.get("is_active")),
        )

    def require_reviewer(self, auth_user_id: str) -> ReviewerContext:
        reviewer = self.reviewer_for_auth_user(auth_user_id)
        if not reviewer:
            raise CalibrationAuthorizationError("No reviewer profile is assigned to this login")
        if not reviewer.is_active:
            raise CalibrationAuthorizationError("Reviewer profile is inactive")
        if not reviewer.consented_at:
            raise CalibrationAuthorizationError("Reviewer consent has not been recorded")
        return reviewer

    def upsert_reviewer(
        self,
        *,
        reviewer_id: str,
        auth_user_id: str,
        display_name: str,
        expertise_level: Optional[str],
        primary_styles: List[str],
        years_experience: Optional[int],
        weighting_factor: float,
        consent_version: str,
        is_active: bool = True,
    ) -> str:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO public.reviewer_profiles (
                        reviewer_id, auth_user_id, display_name, expertise_level,
                        primary_styles_json, years_experience, weighting_factor,
                        consent_version, consented_at, is_active, created_at
                    ) VALUES (
                        :reviewer_id, CAST(:auth_user_id AS uuid), :display_name, :expertise_level,
                        :primary_styles_json, :years_experience, :weighting_factor,
                        :consent_version, NOW(), :is_active, NOW()
                    )
                    ON CONFLICT (reviewer_id) DO UPDATE SET
                        auth_user_id = EXCLUDED.auth_user_id,
                        display_name = EXCLUDED.display_name,
                        expertise_level = EXCLUDED.expertise_level,
                        primary_styles_json = EXCLUDED.primary_styles_json,
                        years_experience = EXCLUDED.years_experience,
                        weighting_factor = EXCLUDED.weighting_factor,
                        consent_version = EXCLUDED.consent_version,
                        consented_at = COALESCE(public.reviewer_profiles.consented_at, NOW()),
                        is_active = EXCLUDED.is_active
                    """
                ),
                {
                    "reviewer_id": reviewer_id,
                    "auth_user_id": auth_user_id,
                    "display_name": display_name,
                    "expertise_level": expertise_level,
                    "primary_styles_json": json.dumps(primary_styles),
                    "years_experience": years_experience,
                    "weighting_factor": float(weighting_factor),
                    "consent_version": consent_version,
                    "is_active": bool(is_active),
                },
            )
        return reviewer_id

    def create_treatment(
        self,
        *,
        drummer_slug: str,
        name: str,
        description: str,
        cfg_overrides: Mapping[str, Any],
        profile_overrides: Mapping[str, Any],
        base_model_version: Optional[str],
        created_by: str,
        status_value: str = "draft",
    ) -> str:
        treatment_id = f"trt_{uuid.uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO public.calibration_treatments (
                        treatment_id, drummer_slug, name, description, status,
                        base_model_version, cfg_overrides_json,
                        profile_overrides_json, created_by, created_at
                    ) VALUES (
                        :treatment_id, :drummer_slug, :name, :description, :status,
                        :base_model_version, CAST(:cfg_overrides AS jsonb),
                        CAST(:profile_overrides AS jsonb), CAST(:created_by AS uuid), NOW()
                    )
                    """
                ),
                {
                    "treatment_id": treatment_id,
                    "drummer_slug": drummer_slug,
                    "name": name,
                    "description": description,
                    "status": status_value,
                    "base_model_version": base_model_version,
                    "cfg_overrides": json.dumps(dict(cfg_overrides or {})),
                    "profile_overrides": json.dumps(dict(profile_overrides or {})),
                    "created_by": created_by,
                },
            )
        return treatment_id

    def get_treatment(self, treatment_id: str) -> Dict[str, Any]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT treatment_id, drummer_slug, name, description, status,
                           base_model_version, cfg_overrides_json,
                           profile_overrides_json, CAST(created_by AS text) AS created_by,
                           CAST(created_at AS text) AS created_at
                    FROM public.calibration_treatments
                    WHERE treatment_id = :treatment_id
                    LIMIT 1
                    """
                ),
                {"treatment_id": treatment_id},
            ).mappings().first()
        if not row:
            raise CalibrationRecordNotFound(f"Treatment not found: {treatment_id}")
        item = dict(row)
        item["cfg_overrides"] = self._json(item.pop("cfg_overrides_json", None), {})
        item["profile_overrides"] = self._json(item.pop("profile_overrides_json", None), {})
        return item

    def create_trial_record(self, payload: Mapping[str, Any]) -> str:
        trial_id = str(payload.get("trial_id") or f"trial_{uuid.uuid4().hex[:16]}")
        params = dict(payload)
        params["trial_id"] = trial_id
        for key in (
            "control_profile_snapshot",
            "challenger_profile_snapshot",
            "assignment_json",
            "generation_metadata",
        ):
            params[key] = json.dumps(params.get(key) or {})

        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO public.calibration_trials (
                        trial_id, item_id, session_id, reviewer_id, drummer_slug,
                        base_groove_id, neutral_run_id, control_run_id,
                        challenger_run_id, visible_a_run_id, visible_b_run_id,
                        challenger_treatment_id, paired_seed, assignment_seed,
                        control_profile_hash, challenger_profile_hash,
                        control_profile_snapshot_json, challenger_profile_snapshot_json,
                        assignment_json, generation_metadata_json,
                        model_version, renderer_version, sample_pack_version,
                        status, created_at
                    ) VALUES (
                        :trial_id, :item_id, :session_id, :reviewer_id, :drummer_slug,
                        :base_groove_id, :neutral_run_id, :control_run_id,
                        :challenger_run_id, :visible_a_run_id, :visible_b_run_id,
                        :challenger_treatment_id, :paired_seed, :assignment_seed,
                        :control_profile_hash, :challenger_profile_hash,
                        CAST(:control_profile_snapshot AS jsonb),
                        CAST(:challenger_profile_snapshot AS jsonb),
                        CAST(:assignment_json AS jsonb),
                        CAST(:generation_metadata AS jsonb),
                        :model_version, :renderer_version, :sample_pack_version,
                        :status, NOW()
                    )
                    """
                ),
                params,
            )
        return trial_id

    def mark_trial_status(self, trial_id: str, status_value: str, error_text: Optional[str] = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE public.calibration_trials
                    SET status = :status,
                        error_text = :error_text,
                        updated_at = NOW()
                    WHERE trial_id = :trial_id
                    """
                ),
                {"trial_id": trial_id, "status": status_value, "error_text": error_text},
            )

    def trial_by_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM public.calibration_trials WHERE item_id = :item_id LIMIT 1"),
                {"item_id": item_id},
            ).mappings().first()
        return dict(row) if row else None

    def refresh_ready_trials_for_reviewer(self, reviewer_id: str) -> int:
        """Mark queued trials ready only after neutral, A, and B have artifacts."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE public.calibration_trials t
                    SET status = 'ready', updated_at = NOW()
                    FROM public.evaluation_sessions s
                    WHERE t.session_id = s.session_id
                      AND s.reviewer_id = :reviewer_id
                      AND t.status = 'queued'
                      AND EXISTS (SELECT 1 FROM public.audio_artifacts a WHERE a.run_id = t.neutral_run_id)
                      AND EXISTS (SELECT 1 FROM public.audio_artifacts a WHERE a.run_id = t.visible_a_run_id)
                      AND EXISTS (SELECT 1 FROM public.audio_artifacts a WHERE a.run_id = t.visible_b_run_id)
                    """
                ),
                {"reviewer_id": reviewer_id},
            )
        return max(0, int(result.rowcount or 0))

    def next_ready_item_for_reviewer(
        self,
        *,
        reviewer_id: str,
        target_drummer_slug: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT i.*, s.reviewer_id, t.trial_id, t.status AS trial_status
                    FROM public.evaluation_items i
                    JOIN public.evaluation_sessions s ON s.session_id = i.session_id
                    JOIN public.calibration_trials t ON t.item_id = i.item_id
                    WHERE s.reviewer_id = :reviewer_id
                      AND s.completed_at IS NULL
                      AND t.status = 'ready'
                      AND (:slug = '' OR i.target_drummer_slug = :slug)
                      AND NOT EXISTS (
                          SELECT 1 FROM public.pairwise_judgments j
                          WHERE j.item_id = i.item_id AND j.reviewer_id = :reviewer_id
                      )
                    ORDER BY s.assigned_at ASC NULLS LAST, i.created_at ASC
                    LIMIT 1
                    """
                ),
                {"reviewer_id": reviewer_id, "slug": str(target_drummer_slug or "").strip()},
            ).mappings().first()
        return dict(row) if row else None

    def require_item_ownership(self, *, item_id: str, reviewer_id: str) -> Dict[str, Any]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT i.*, s.reviewer_id, t.trial_id, t.status AS trial_status
                    FROM public.evaluation_items i
                    JOIN public.evaluation_sessions s ON s.session_id = i.session_id
                    JOIN public.calibration_trials t ON t.item_id = i.item_id
                    WHERE i.item_id = :item_id AND s.reviewer_id = :reviewer_id
                    LIMIT 1
                    """
                ),
                {"item_id": item_id, "reviewer_id": reviewer_id},
            ).mappings().first()
        if not row:
            raise CalibrationAuthorizationError("Item is not assigned to this reviewer")
        return dict(row)

    def mark_session_started(self, session_id: str) -> None:
        session = str(session_id or "").strip()
        if not session:
            return
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE public.evaluation_sessions
                    SET started_at = COALESCE(started_at, NOW())
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session},
            )

    def submit_review(
        self,
        *,
        item_id: str,
        reviewer_id: str,
        judgment: Mapping[str, Any],
        ratings_by_label: Mapping[str, Mapping[str, int]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Persist one complete reviewer response in a single Postgres transaction.

        The endpoint uses one stable client idempotency key.  A lost HTTP response
        can therefore be retried without creating a second judgment or a partial
        A/B rating pair.  The unique reviewer/item indexes added by the migration
        also prevent duplicates when a client accidentally changes its key.
        """
        item = str(item_id or "").strip()
        reviewer = str(reviewer_id or "").strip()
        root_key = str(idempotency_key or "").strip()
        if not item or not reviewer:
            raise ValueError("item_id and reviewer_id are required")
        if not root_key:
            raise ValueError("Idempotency-Key is required")
        if len(root_key) > 240:
            raise ValueError("Idempotency-Key exceeds 240 characters")

        rating_names = (
            "stylistic_authenticity",
            "groove_feel",
            "dynamics",
            "phrasing",
            "kit_balance",
            "fill_behavior",
            "human_realism",
            "overall_usefulness",
        )
        normalized_ratings: Dict[str, Dict[str, int]] = {}
        for raw_label, raw_values in dict(ratings_by_label or {}).items():
            label = str(raw_label).strip().upper()
            if label not in {"A", "B"}:
                raise ValueError("Rating candidate label must be A or B")
            values = {name: int(round(float(raw_values[name]))) for name in rating_names}
            if any(value < 1 or value > 10 for value in values.values()):
                raise ValueError("All rating values must be between 1 and 10")
            normalized_ratings[label] = values

        preferred = str(judgment.get("preferred_candidate") or "").strip()
        closer = str(judgment.get("closer_to_target") or "").strip()
        better_feel = str(judgment.get("better_feel") or "").strip()
        more_musical = str(judgment.get("more_musical") or "").strip()
        allowed_choices = {"A", "B", "tie", "neither"}
        if any(value not in allowed_choices for value in (preferred, closer, better_feel, more_musical)):
            raise ValueError("Judgment choices must be A, B, tie, or neither")

        technical_issue = bool(judgment.get("technical_issue"))
        cannot_judge = bool(judgment.get("cannot_judge"))
        if not technical_issue and not cannot_judge and set(normalized_ratings) != {"A", "B"}:
            raise ValueError("Complete A and B ratings are required for a normal review")

        judgment_id = f"judge_{uuid.uuid4().hex[:16]}"
        rating_ids: Dict[str, str] = {}
        with self._engine.begin() as conn:
            ownership = conn.execute(
                text(
                    """
                    SELECT i.session_id, t.trial_id, t.status AS trial_status
                    FROM public.evaluation_items i
                    JOIN public.evaluation_sessions s ON s.session_id = i.session_id
                    JOIN public.calibration_trials t ON t.item_id = i.item_id
                    WHERE i.item_id = :item_id
                      AND s.reviewer_id = :reviewer_id
                    FOR UPDATE OF t, s
                    """
                ),
                {"item_id": item, "reviewer_id": reviewer},
            ).mappings().first()
            if not ownership:
                raise CalibrationAuthorizationError("Item is not assigned to this reviewer")
            if str(ownership.get("trial_status") or "") not in {"ready", "completed"}:
                raise CalibrationAuthorizationError("Trial is not ready for review")

            row = conn.execute(
                text(
                    """
                    INSERT INTO public.pairwise_judgments (
                        judgment_id, item_id, reviewer_id, preferred_candidate,
                        closer_to_target, better_feel, more_musical, confidence,
                        technical_issue, cannot_judge, comment, listening_ms,
                        candidate_a_listening_ms, candidate_b_listening_ms,
                        candidate_a_play_count, candidate_b_play_count,
                        idempotency_key, created_at
                    ) VALUES (
                        :judgment_id, :item_id, :reviewer_id, :preferred_candidate,
                        :closer_to_target, :better_feel, :more_musical, :confidence,
                        :technical_issue, :cannot_judge, :comment, :listening_ms,
                        :candidate_a_listening_ms, :candidate_b_listening_ms,
                        :candidate_a_play_count, :candidate_b_play_count,
                        :idempotency_key, NOW()
                    )
                    ON CONFLICT (reviewer_id, item_id)
                      WHERE reviewer_id IS NOT NULL AND idempotency_key IS NOT NULL
                    DO UPDATE SET judgment_id = public.pairwise_judgments.judgment_id
                    RETURNING judgment_id
                    """
                ),
                {
                    "judgment_id": judgment_id,
                    "item_id": item,
                    "reviewer_id": reviewer,
                    "preferred_candidate": preferred,
                    "closer_to_target": closer,
                    "better_feel": better_feel,
                    "more_musical": more_musical,
                    "confidence": int(judgment.get("confidence") or 0),
                    "technical_issue": technical_issue,
                    "cannot_judge": cannot_judge,
                    "comment": judgment.get("comment"),
                    "listening_ms": max(0, int(judgment.get("listening_ms") or 0)),
                    "candidate_a_listening_ms": max(0, int(judgment.get("candidate_a_listening_ms") or 0)),
                    "candidate_b_listening_ms": max(0, int(judgment.get("candidate_b_listening_ms") or 0)),
                    "candidate_a_play_count": max(0, int(judgment.get("candidate_a_play_count") or 0)),
                    "candidate_b_play_count": max(0, int(judgment.get("candidate_b_play_count") or 0)),
                    "idempotency_key": f"{root_key}:judgment",
                },
            ).first()
            stored_judgment_id = str(row[0]) if row else judgment_id

            for label, values in sorted(normalized_ratings.items()):
                rating_id = f"rate_{uuid.uuid4().hex[:16]}"
                row = conn.execute(
                    text(
                        """
                        INSERT INTO public.attribute_ratings (
                            rating_id, item_id, reviewer_id, candidate_label,
                            stylistic_authenticity, groove_feel, dynamics, phrasing,
                            kit_balance, fill_behavior, human_realism,
                            overall_usefulness, idempotency_key, created_at
                        ) VALUES (
                            :rating_id, :item_id, :reviewer_id, :candidate_label,
                            :stylistic_authenticity, :groove_feel, :dynamics, :phrasing,
                            :kit_balance, :fill_behavior, :human_realism,
                            :overall_usefulness, :idempotency_key, NOW()
                        )
                        ON CONFLICT (reviewer_id, item_id, candidate_label)
                          WHERE reviewer_id IS NOT NULL AND idempotency_key IS NOT NULL
                        DO UPDATE SET rating_id = public.attribute_ratings.rating_id
                        RETURNING rating_id
                        """
                    ),
                    {
                        "rating_id": rating_id,
                        "item_id": item,
                        "reviewer_id": reviewer,
                        "candidate_label": label,
                        "idempotency_key": f"{root_key}:rating:{label}",
                        **values,
                    },
                ).first()
                rating_ids[label] = str(row[0]) if row else rating_id

            conn.execute(
                text(
                    """
                    UPDATE public.calibration_trials
                    SET status = 'completed', updated_at = NOW()
                    WHERE item_id = :item_id
                    """
                ),
                {"item_id": item},
            )
            conn.execute(
                text(
                    """
                    UPDATE public.evaluation_sessions
                    SET started_at = COALESCE(started_at, NOW()),
                        completed_at = COALESCE(completed_at, NOW())
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": str(ownership["session_id"])},
            )

        return {
            "judgment_id": stored_judgment_id,
            "rating_ids": rating_ids,
            "trial_id": str(ownership["trial_id"]),
            "status": "completed",
        }

    def submit_judgment(
        self,
        *,
        item_id: str,
        reviewer_id: str,
        preferred_candidate: str,
        closer_to_target: str,
        better_feel: str,
        more_musical: str,
        confidence: int,
        technical_issue: bool,
        cannot_judge: bool,
        comment: Optional[str],
        listening_ms: int,
        candidate_a_listening_ms: int,
        candidate_b_listening_ms: int,
        candidate_a_play_count: int,
        candidate_b_play_count: int,
        idempotency_key: str,
    ) -> str:
        self.require_item_ownership(item_id=item_id, reviewer_id=reviewer_id)
        judgment_id = f"judge_{uuid.uuid4().hex[:16]}"
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO public.pairwise_judgments (
                        judgment_id, item_id, reviewer_id, preferred_candidate,
                        closer_to_target, better_feel, more_musical, confidence,
                        technical_issue, cannot_judge, comment, listening_ms,
                        candidate_a_listening_ms, candidate_b_listening_ms,
                        candidate_a_play_count, candidate_b_play_count,
                        idempotency_key, created_at
                    ) VALUES (
                        :judgment_id, :item_id, :reviewer_id, :preferred_candidate,
                        :closer_to_target, :better_feel, :more_musical, :confidence,
                        :technical_issue, :cannot_judge, :comment, :listening_ms,
                        :candidate_a_listening_ms, :candidate_b_listening_ms,
                        :candidate_a_play_count, :candidate_b_play_count,
                        :idempotency_key, NOW()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        idempotency_key = EXCLUDED.idempotency_key
                    RETURNING judgment_id
                    """
                ),
                {
                    "judgment_id": judgment_id,
                    "item_id": item_id,
                    "reviewer_id": reviewer_id,
                    "preferred_candidate": preferred_candidate,
                    "closer_to_target": closer_to_target,
                    "better_feel": better_feel,
                    "more_musical": more_musical,
                    "confidence": int(confidence),
                    "technical_issue": bool(technical_issue),
                    "cannot_judge": bool(cannot_judge),
                    "comment": comment,
                    "listening_ms": max(0, int(listening_ms)),
                    "candidate_a_listening_ms": max(0, int(candidate_a_listening_ms)),
                    "candidate_b_listening_ms": max(0, int(candidate_b_listening_ms)),
                    "candidate_a_play_count": max(0, int(candidate_a_play_count)),
                    "candidate_b_play_count": max(0, int(candidate_b_play_count)),
                    "idempotency_key": idempotency_key,
                },
            ).first()
        return str(row[0]) if row else judgment_id

    def submit_rating(
        self,
        *,
        item_id: str,
        reviewer_id: str,
        candidate_label: str,
        ratings: Mapping[str, int],
        idempotency_key: str,
    ) -> str:
        self.require_item_ownership(item_id=item_id, reviewer_id=reviewer_id)
        rating_id = f"rate_{uuid.uuid4().hex[:16]}"
        fields = {
            key: int(round(float(ratings[key])))
            for key in (
                "stylistic_authenticity",
                "groove_feel",
                "dynamics",
                "phrasing",
                "kit_balance",
                "fill_behavior",
                "human_realism",
                "overall_usefulness",
            )
        }
        with self._engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO public.attribute_ratings (
                        rating_id, item_id, reviewer_id, candidate_label,
                        stylistic_authenticity, groove_feel, dynamics, phrasing,
                        kit_balance, fill_behavior, human_realism,
                        overall_usefulness, idempotency_key, created_at
                    ) VALUES (
                        :rating_id, :item_id, :reviewer_id, :candidate_label,
                        :stylistic_authenticity, :groove_feel, :dynamics, :phrasing,
                        :kit_balance, :fill_behavior, :human_realism,
                        :overall_usefulness, :idempotency_key, NOW()
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        idempotency_key = EXCLUDED.idempotency_key
                    RETURNING rating_id
                    """
                ),
                {
                    "rating_id": rating_id,
                    "item_id": item_id,
                    "reviewer_id": reviewer_id,
                    "candidate_label": candidate_label,
                    "idempotency_key": idempotency_key,
                    **fields,
                },
            ).first()
        return str(row[0]) if row else rating_id

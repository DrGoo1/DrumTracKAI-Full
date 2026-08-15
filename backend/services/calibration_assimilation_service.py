"""Bridge Admin-app assimilation data into the Calibration v2 reviewer flow.

The Admin app extracts drummer identity from signature-song drum stems and writes
Postgres rollups plus detailed timing, dynamics, fill, phrase, cymbal, limb, and
Phase 32-42 features.  This service makes those assimilated models discoverable
to Calibration v2 and can provision a blinded trial from an approved Phase 6
preset treatment.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import secrets
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from sqlalchemy import text

from admin.services.central_database_service import CentralDatabaseService
from backend.services.calibration_profile_resolver import (
    CalibrationProfileResolver,
    CalibrationProfileUnavailable,
    ResolvedCalibrationProfile,
)
from backend.services.calibration_production_engine import validate_cfg_overrides
from backend.services.calibration_trial_service import CalibrationTrialService, TrialCreateInput
from backend.services.calibration_v2_repository import CalibrationV2Repository


class CalibrationProvisioningUnavailable(RuntimeError):
    """Raised when an assimilated model cannot yet be provisioned for review."""


@dataclass(frozen=True)
class AssimilationModelStatus:
    drummer_slug: str
    display_name: str
    profile_resolved: bool
    model_ready: bool
    can_queue_trial: bool
    rollup_version: Optional[str]
    profile_snapshot_hash: Optional[str]
    source_counts: Dict[str, int]
    source_song_count: int
    source_stem_count: int
    hit_event_count: int
    fill_event_count: int
    technique_event_count: int
    assimilation_score: int
    profile_sections: Dict[str, bool]
    active_treatment_id: Optional[str]
    ready_trial_count: int
    queued_trial_count: int
    blockers: List[str]

    def reviewer_payload(self) -> Dict[str, Any]:
        """Return the non-sensitive model catalog information exposed to reviewers."""
        return {
            "drummer_slug": self.drummer_slug,
            "display_name": self.display_name,
            "ready_trial_count": self.ready_trial_count,
            "queued_trial_count": self.queued_trial_count,
            "model_ready": self.model_ready,
            "can_queue_trial": self.can_queue_trial,
            "source_song_count": self.source_song_count,
            "assimilation_score": self.assimilation_score,
            "rollup_version": self.rollup_version,
            "blockers": list(self.blockers),
        }

    def admin_payload(self) -> Dict[str, Any]:
        return {
            **self.reviewer_payload(),
            "profile_resolved": self.profile_resolved,
            "profile_snapshot_hash": self.profile_snapshot_hash,
            "source_counts": dict(self.source_counts),
            "source_stem_count": self.source_stem_count,
            "hit_event_count": self.hit_event_count,
            "fill_event_count": self.fill_event_count,
            "technique_event_count": self.technique_event_count,
            "profile_sections": dict(self.profile_sections),
            "active_treatment_id": self.active_treatment_id,
        }


class CalibrationAssimilationService:
    """Catalog and provision Calibration v2 trials from Admin-app assimilation."""

    _COUNT_TABLES = {
        "analysis_artifacts": "source_artifact_count",
        "stem_artifacts": "source_stem_count",
        "drum_hit_events": "hit_event_count",
        "fill_events": "fill_event_count",
        "technique_events": "technique_event_count",
    }

    def __init__(
        self,
        db: CentralDatabaseService,
        *,
        repository: Optional[CalibrationV2Repository] = None,
        resolver: Optional[CalibrationProfileResolver] = None,
        trial_service: Optional[CalibrationTrialService] = None,
    ) -> None:
        self._db = db
        self._engine = getattr(db, "_engine", None)
        if self._engine is None:
            raise RuntimeError("Calibration assimilation catalog requires Postgres")
        self._repo = repository or CalibrationV2Repository(db)
        self._resolver = resolver or CalibrationProfileResolver(db)
        self._trial_service = trial_service or CalibrationTrialService(db, repository=self._repo)
        self._columns_cache: Dict[str, Set[str]] = {}

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

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, float(value)))

    def _table_columns(self, table_name: str) -> Set[str]:
        table = str(table_name or "").strip()
        if table in self._columns_cache:
            return set(self._columns_cache[table])
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = :table_name
                    """
                ),
                {"table_name": table},
            ).all()
        columns = {str(row[0]) for row in rows if row and row[0]}
        self._columns_cache[table] = columns
        return set(columns)

    def _drummer_keys(self, drummer_slug: str) -> List[str]:
        slug = str(drummer_slug or "").strip()
        keys = [slug] if slug else []
        try:
            record = self._db.get_drummer(slug)
        except Exception:
            record = None
        if isinstance(record, dict):
            for field_name in ("id", "drummer_id", "slug", "persona_id"):
                value = str(record.get(field_name) or "").strip()
                if value and value not in keys:
                    keys.append(value)
        return keys

    @staticmethod
    def _key_predicate(column_name: str, keys: Sequence[str]) -> tuple[str, Dict[str, Any]]:
        params = {f"drummer_key_{index}": value for index, value in enumerate(keys)}
        clauses = [
            f"CAST({column_name} AS TEXT) = :drummer_key_{index}"
            for index in range(len(keys))
        ]
        return " OR ".join(clauses) or "FALSE", params

    def _rollup_slugs(self) -> List[str]:
        rollup_columns = self._table_columns("drummer_profile_rollups")
        if "drummer_id" not in rollup_columns:
            return []
        drummer_columns = self._table_columns("drummers")
        with self._engine.connect() as conn:
            if {"id", "drummer_id"}.issubset(drummer_columns):
                rows = conn.execute(
                    text(
                        """
                        SELECT DISTINCT COALESCE(
                            NULLIF(BTRIM(CAST(d.drummer_id AS TEXT)), ''),
                            CAST(r.drummer_id AS TEXT)
                        ) AS drummer_slug
                        FROM public.drummer_profile_rollups r
                        LEFT JOIN public.drummers d
                          ON CAST(d.id AS TEXT) = CAST(r.drummer_id AS TEXT)
                          OR CAST(d.drummer_id AS TEXT) = CAST(r.drummer_id AS TEXT)
                        ORDER BY drummer_slug
                        """
                    )
                ).all()
            else:
                rows = conn.execute(
                    text(
                        """
                        SELECT DISTINCT CAST(drummer_id AS TEXT) AS drummer_slug
                        FROM public.drummer_profile_rollups
                        ORDER BY drummer_slug
                        """
                    )
                ).all()
        return [str(row[0]).strip() for row in rows if row and str(row[0] or "").strip()]

    def _analysis_evidence(self, drummer_slug: str) -> Dict[str, int]:
        keys = self._drummer_keys(drummer_slug)
        if not keys:
            return {
                "source_song_count": 0,
                "source_artifact_count": 0,
                "source_stem_count": 0,
                "hit_event_count": 0,
                "fill_event_count": 0,
                "technique_event_count": 0,
            }

        analysis_columns = self._table_columns("song_performance_analysis")
        if not {"analysis_id", "drummer_id"}.issubset(analysis_columns):
            analysis_ids: List[str] = []
        else:
            predicate, params = self._key_predicate("drummer_id", keys)
            with self._engine.connect() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT CAST(analysis_id AS TEXT)
                        FROM public.song_performance_analysis
                        WHERE ({predicate})
                        """
                    ),
                    params,
                ).all()
            analysis_ids = [str(row[0]) for row in rows if row and row[0] is not None]

        evidence = {
            "source_song_count": len(set(analysis_ids)),
            "source_artifact_count": 0,
            "source_stem_count": 0,
            "hit_event_count": 0,
            "fill_event_count": 0,
            "technique_event_count": 0,
        }
        for table_name, output_key in self._COUNT_TABLES.items():
            columns = self._table_columns(table_name)
            if not columns:
                continue
            if "analysis_id" in columns and analysis_ids:
                params = {f"analysis_{index}": value for index, value in enumerate(analysis_ids)}
                placeholders = ", ".join(f":analysis_{index}" for index in range(len(analysis_ids)))
                sql = f"SELECT COUNT(*) FROM public.{table_name} WHERE CAST(analysis_id AS TEXT) IN ({placeholders})"
            elif "drummer_id" in columns:
                predicate, params = self._key_predicate("drummer_id", keys)
                sql = f"SELECT COUNT(*) FROM public.{table_name} WHERE ({predicate})"
            else:
                continue
            with self._engine.connect() as conn:
                count = conn.execute(text(sql), params).scalar()
            evidence[output_key] = self._safe_int(count)
        return evidence

    @staticmethod
    def _profile_sections(profile: Mapping[str, Any]) -> Dict[str, bool]:
        phase = profile.get("phase32_42_features") if isinstance(profile, Mapping) else None
        phase37 = phase.get("phase37_42") if isinstance(phase, Mapping) else None
        personality = phase37.get("drummer_personality_profile") if isinstance(phase37, Mapping) else None
        return {
            "microtiming": bool(
                profile.get("instrument_timing_profiles")
                or profile.get("timing_tightness") is not None
                or (isinstance(phase37, Mapping) and phase37.get("microtiming_profile"))
            ),
            "dynamics": bool(
                profile.get("instrument_dynamic_profiles")
                or profile.get("velocity_mean") is not None
                or profile.get("humanness") is not None
            ),
            "fills": bool(profile.get("fill_behavior") or profile.get("fills_per_min") is not None),
            "phrases": bool(profile.get("phrase_features") or (isinstance(phase37, Mapping) and phase37.get("phrase_continuity_memory"))),
            "cymbal_language": bool(profile.get("cymbal_language")),
            "limb_coordination": bool(profile.get("limb_coordination") or (isinstance(phase37, Mapping) and phase37.get("limb_interaction_profile"))),
            "personality": bool(personality or profile.get("persona")),
        }

    def _assimilation_score(
        self,
        *,
        profile: Mapping[str, Any],
        evidence: Mapping[str, int],
    ) -> int:
        calculator = getattr(self._db, "_compute_assimilation_score", None)
        if callable(calculator):
            try:
                return int(
                    calculator(
                        songs=int(evidence.get("source_song_count", 0)),
                        artifacts=int(evidence.get("source_artifact_count", 0)),
                        stems=int(evidence.get("source_stem_count", 0)),
                        hit_events=int(evidence.get("hit_event_count", 0)),
                        fills=int(evidence.get("fill_event_count", 0)),
                        techniques=int(evidence.get("technique_event_count", 0)),
                        pocket_tightness=profile.get("pocket_tightness"),
                        humanness=profile.get("humanness"),
                    )
                )
            except Exception:
                pass
        songs = int(evidence.get("source_song_count", 0))
        score = min(50.0, (songs / 20.0) * 50.0)
        richness = 0.0
        if evidence.get("source_artifact_count", 0) > 0:
            richness += 10.0
        stems = int(evidence.get("source_stem_count", 0))
        richness += 15.0 if stems >= 6 else (7.0 if stems > 0 else 0.0)
        if evidence.get("hit_event_count", 0) > 0:
            richness += 25.0
        if evidence.get("fill_event_count", 0) > 0:
            richness += 10.0
        if evidence.get("technique_event_count", 0) > 0:
            richness += 10.0
        return int(round(self._clamp(score + min(50.0, richness), 0.0, 100.0)))

    def _active_treatment(self, drummer_slug: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT treatment_id, drummer_slug, name, description,
                           base_model_version, cfg_overrides_json,
                           profile_overrides_json, CAST(created_at AS text) AS created_at
                    FROM public.calibration_treatments
                    WHERE drummer_slug = :drummer_slug
                      AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"drummer_slug": drummer_slug},
            ).mappings().first()
        if not row:
            return None
        item = dict(row)
        item["cfg_overrides"] = self._json(item.pop("cfg_overrides_json", None), {})
        item["profile_overrides"] = self._json(item.pop("profile_overrides_json", None), {})
        return item

    def _trial_counts(self, *, reviewer_id: Optional[str], drummer_slug: str) -> Dict[str, int]:
        if not reviewer_id:
            return {"ready_trial_count": 0, "queued_trial_count": 0}
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT t.status, COUNT(*) AS item_count
                    FROM public.calibration_trials t
                    JOIN public.evaluation_sessions s ON s.session_id = t.session_id
                    WHERE s.reviewer_id = :reviewer_id
                      AND t.drummer_slug = :drummer_slug
                      AND t.status IN ('queued', 'ready')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.pairwise_judgments j
                          WHERE j.item_id = t.item_id
                            AND j.reviewer_id = :reviewer_id
                      )
                    GROUP BY t.status
                    """
                ),
                {"reviewer_id": reviewer_id, "drummer_slug": drummer_slug},
            ).mappings().all()
        counts = {str(row["status"]): self._safe_int(row.get("item_count")) for row in rows}
        return {
            "ready_trial_count": counts.get("ready", 0),
            "queued_trial_count": counts.get("queued", 0),
        }

    def _open_trial(self, *, reviewer_id: str, drummer_slug: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT t.trial_id, t.item_id, t.session_id, t.status,
                           t.created_at, t.updated_at
                    FROM public.calibration_trials t
                    JOIN public.evaluation_sessions s ON s.session_id = t.session_id
                    WHERE s.reviewer_id = :reviewer_id
                      AND t.drummer_slug = :drummer_slug
                      AND t.status IN ('queued', 'ready')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.pairwise_judgments j
                          WHERE j.item_id = t.item_id
                            AND j.reviewer_id = :reviewer_id
                      )
                    ORDER BY CASE WHEN t.status = 'ready' THEN 0 ELSE 1 END,
                             t.created_at ASC
                    LIMIT 1
                    """
                ),
                {"reviewer_id": reviewer_id, "drummer_slug": drummer_slug},
            ).mappings().first()
        return dict(row) if row else None

    def model_status(
        self,
        drummer_slug: str,
        *,
        reviewer_id: Optional[str] = None,
    ) -> AssimilationModelStatus:
        slug = str(drummer_slug or "").strip()
        if not slug:
            raise ValueError("drummer_slug is required")

        blockers: List[str] = []
        resolved: Optional[ResolvedCalibrationProfile] = None
        try:
            resolved = self._resolver.resolve(drummer_slug=slug, strict=True)
        except CalibrationProfileUnavailable as exc:
            blockers.append(str(exc))
        except Exception as exc:
            blockers.append(f"Assimilation profile resolution failed: {exc}")

        evidence = self._analysis_evidence(slug)
        profile = resolved.profile if resolved else {}
        sections = self._profile_sections(profile)
        populated_sections = sum(1 for value in sections.values() if value)
        minimum_songs = max(1, self._safe_int(os.getenv("CALIBRATION_MIN_ASSIMILATED_SONGS", "1"), 1))
        minimum_hits = max(1, self._safe_int(os.getenv("CALIBRATION_MIN_HIT_EVENTS", "1"), 1))
        minimum_sections = max(1, self._safe_int(os.getenv("CALIBRATION_MIN_PROFILE_SECTIONS", "2"), 2))

        if evidence["source_song_count"] < minimum_songs:
            blockers.append(
                f"Only {evidence['source_song_count']} analyzed signature songs; minimum is {minimum_songs}"
            )
        if evidence["hit_event_count"] < minimum_hits:
            blockers.append(
                f"Only {evidence['hit_event_count']} extracted drum hits; minimum is {minimum_hits}"
            )
        if populated_sections < minimum_sections:
            blockers.append(
                f"Only {populated_sections} personality feature sections are populated; minimum is {minimum_sections}"
            )

        active_treatment = self._active_treatment(slug)
        if not active_treatment:
            blockers.append("No active Calibration v2 treatment is configured")

        counts = self._trial_counts(reviewer_id=reviewer_id, drummer_slug=slug)
        profile_blockers = [item for item in blockers if "treatment" not in item.lower()]
        model_ready = bool(resolved) and not profile_blockers
        can_queue = model_ready and active_treatment is not None

        return AssimilationModelStatus(
            drummer_slug=slug,
            display_name=self._repo.drummer_display_name(slug),
            profile_resolved=resolved is not None,
            model_ready=model_ready,
            can_queue_trial=can_queue,
            rollup_version=resolved.rollup_version if resolved else None,
            profile_snapshot_hash=resolved.snapshot_hash if resolved else None,
            source_counts=dict(resolved.source_counts if resolved else {}),
            source_song_count=evidence["source_song_count"],
            source_stem_count=evidence["source_stem_count"],
            hit_event_count=evidence["hit_event_count"],
            fill_event_count=evidence["fill_event_count"],
            technique_event_count=evidence["technique_event_count"],
            assimilation_score=self._assimilation_score(profile=profile, evidence=evidence),
            profile_sections=sections,
            active_treatment_id=(str(active_treatment["treatment_id"]) if active_treatment else None),
            ready_trial_count=counts["ready_trial_count"],
            queued_trial_count=counts["queued_trial_count"],
            blockers=blockers,
        )

    def list_models(
        self,
        *,
        reviewer_id: Optional[str] = None,
        include_blocked: bool = False,
    ) -> List[AssimilationModelStatus]:
        statuses = [
            self.model_status(slug, reviewer_id=reviewer_id)
            for slug in self._rollup_slugs()
        ]
        if not include_blocked:
            statuses = [
                item
                for item in statuses
                if item.model_ready
                and (
                    item.can_queue_trial
                    or item.ready_trial_count > 0
                    or item.queued_trial_count > 0
                )
            ]
        return sorted(statuses, key=lambda item: item.display_name.lower())

    def _load_phase6_preset(self, drummer_slug: str, profile: Mapping[str, Any]) -> Dict[str, Any]:
        candidates: List[str] = []
        for value in (
            profile.get("preset_id"),
            (profile.get("preset") or {}).get("id") if isinstance(profile.get("preset"), Mapping) else None,
            f"phase6_{drummer_slug}".lower(),
        ):
            text_value = str(value or "").strip()
            if text_value and text_value not in candidates:
                candidates.append(text_value)
        if not candidates:
            raise CalibrationProvisioningUnavailable(
                f"Admin Phase 6 has not generated a preset for '{drummer_slug}'"
            )
        params = {f"preset_{index}": value for index, value in enumerate(candidates)}
        placeholders = ", ".join(f":preset_{index}" for index in range(len(candidates)))
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT preset_id, name, tier, deltas_json, policies_json,
                           source_type, source_song_name, source_ref
                    FROM public.drummer_presets
                    WHERE preset_id IN ({placeholders})
                    ORDER BY CASE WHEN preset_id = :preferred THEN 0 ELSE 1 END,
                             created_at DESC
                    LIMIT 1
                    """
                ),
                {**params, "preferred": candidates[0]},
            ).mappings().first()
        if not row:
            raise CalibrationProvisioningUnavailable(
                f"Admin Phase 6 preset row was not found for '{drummer_slug}'"
            )
        item = dict(row)
        item["deltas"] = self._json(item.pop("deltas_json", None), {})
        item["policies"] = self._json(item.pop("policies_json", None), {})
        return item

    def bootstrap_phase6_treatment(
        self,
        *,
        drummer_slug: str,
        created_by: str,
    ) -> Dict[str, Any]:
        slug = str(drummer_slug or "").strip()
        current = self._active_treatment(slug)
        if current:
            return {"created": False, "treatment": current}

        resolved = self._resolver.resolve(drummer_slug=slug, strict=True)
        preset = self._load_phase6_preset(slug, resolved.profile)
        cfg_overrides = validate_cfg_overrides(preset.get("deltas") or {})
        if not cfg_overrides:
            raise CalibrationProvisioningUnavailable(
                f"Admin Phase 6 preset for '{slug}' contains no usable generation deltas"
            )

        treatment_id = self._repo.create_treatment(
            drummer_slug=slug,
            name=f"Assimilation Phase 6 — {preset.get('name') or slug}",
            description=(
                "Control uses the complete assimilated drummer profile. Challenger applies "
                f"the Admin Phase 6 preset '{preset.get('preset_id')}' derived from signature-song analyses."
            ),
            cfg_overrides=cfg_overrides,
            profile_overrides={},
            base_model_version=resolved.rollup_version,
            created_by=created_by,
            status_value="active",
        )
        treatment = self._active_treatment(slug)
        return {
            "created": True,
            "treatment": treatment or {
                "treatment_id": treatment_id,
                "drummer_slug": slug,
                "cfg_overrides": cfg_overrides,
            },
        }

    def ensure_reviewer_trial(
        self,
        *,
        reviewer_id: str,
        drummer_slug: str,
    ) -> Dict[str, Any]:
        reviewer = str(reviewer_id or "").strip()
        slug = str(drummer_slug or "").strip()
        if not reviewer or not slug:
            raise ValueError("reviewer_id and drummer_slug are required")

        existing = self._open_trial(reviewer_id=reviewer, drummer_slug=slug)
        if existing:
            return {
                "status": str(existing.get("status") or "queued"),
                "trial_id": str(existing.get("trial_id") or ""),
                "item_id": str(existing.get("item_id") or ""),
                "session_id": str(existing.get("session_id") or ""),
                "created": False,
            }

        status = self.model_status(slug, reviewer_id=reviewer)
        if not status.model_ready:
            raise CalibrationProvisioningUnavailable(
                f"Assimilated drummer model '{slug}' is not calibration-ready: "
                + "; ".join(status.blockers)
            )
        if not status.active_treatment_id:
            raise CalibrationProvisioningUnavailable(
                f"No active treatment exists for '{slug}'. Run the Admin Phase 6 treatment bootstrap first."
            )

        result = self._trial_service.create_trial(
            TrialCreateInput(
                reviewer_id=reviewer,
                drummer_slug=slug,
                base_groove_id=str(
                    os.getenv("CALIBRATION_DEFAULT_BASE_GROOVE_ID", "base_groove")
                ).strip()
                or "base_groove",
                challenger_treatment_id=status.active_treatment_id,
                paired_seed=secrets.randbelow(2_000_000_000),
                assignment_seed=secrets.randbelow(2_000_000_000),
                repeats=max(1, self._safe_int(os.getenv("CALIBRATION_DEFAULT_REPEATS", "4"), 4)),
                render_profile_id=str(
                    os.getenv("CALIBRATION_RENDER_PROFILE_ID", "calibration_standard_v2")
                ).strip()
                or "calibration_standard_v2",
                sample_pack_version=str(
                    os.getenv("CALIBRATION_SAMPLE_PACK_VERSION", "default")
                ).strip()
                or "default",
                kit_id=str(os.getenv("CALIBRATION_KIT_ID", "default_kit")).strip()
                or "default_kit",
            )
        )
        return {**result, "created": True}

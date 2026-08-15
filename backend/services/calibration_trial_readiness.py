from __future__ import annotations

"""Strict reviewer-readiness gate for Calibration v2 trials."""

from dataclasses import dataclass
from typing import Any, Dict

from sqlalchemy import text

from admin.services.central_database_service import CentralDatabaseService


@dataclass(frozen=True)
class ReadinessRefreshResult:
    ready_count: int
    invalidated_count: int


class CalibrationTrialReadinessService:
    """Promote only reproducible, durable, non-diagnostic trials to ready."""

    def __init__(self, db: CentralDatabaseService) -> None:
        self._engine = getattr(db, "_engine", None)
        if self._engine is None:
            raise RuntimeError("Calibration trial readiness requires Postgres")

    @staticmethod
    def _json_array(column_sql: str) -> str:
        """Return a TEXT/JSONB-compatible expression normalized to a JSON array."""

        return (
            "CASE "
            f"WHEN {column_sql} IS NULL "
            f"  OR BTRIM({column_sql}::text) IN ('', 'null') "
            "THEN '[]'::jsonb "
            f"ELSE {column_sql}::jsonb END"
        )

    @staticmethod
    def _json_object(column_sql: str) -> str:
        """Return a TEXT/JSONB-compatible expression normalized to an object."""

        return (
            "CASE "
            f"WHEN {column_sql} IS NULL "
            f"  OR BTRIM({column_sql}::text) IN ('', 'null') "
            "THEN '{}'::jsonb "
            f"ELSE {column_sql}::jsonb END"
        )

    @classmethod
    def _strict_predicate(cls) -> str:
        # Every run must have durable events, a completed durable job and an
        # approved S3/HTTPS artifact with complete renderer provenance.
        events_json = cls._json_array("e.event_stream_json")
        artifact_ids_json = cls._json_array("j.artifact_ids_json")
        recipe_json = cls._json_object("a.render_recipe_json")
        return f"""
            LOWER(COALESCE(
                t.generation_metadata_json #>> '{{control,production_metadata,backend}}',
                ''
            )) = 'onnx'
            AND LOWER(COALESCE(
                t.generation_metadata_json #>> '{{challenger,production_metadata,backend}}',
                ''
            )) = 'onnx'
            AND COALESCE(t.renderer_version, '') NOT IN ('', 'unknown')
            AND COALESCE(t.sample_pack_version, '') NOT IN ('', 'default', 'unknown')

            AND EXISTS (
                SELECT 1
                FROM public.calibration_run_events e
                WHERE e.run_id = t.neutral_run_id
                  AND jsonb_typeof({events_json}) = 'array'
                  AND jsonb_array_length({events_json}) > 0
            )
            AND EXISTS (
                SELECT 1
                FROM public.calibration_run_events e
                WHERE e.run_id = t.control_run_id
                  AND jsonb_typeof({events_json}) = 'array'
                  AND jsonb_array_length({events_json}) > 0
            )
            AND EXISTS (
                SELECT 1
                FROM public.calibration_run_events e
                WHERE e.run_id = t.challenger_run_id
                  AND jsonb_typeof({events_json}) = 'array'
                  AND jsonb_array_length({events_json}) > 0
            )

            AND EXISTS (
                SELECT 1
                FROM public.calibration_render_jobs j
                WHERE j.run_id = t.neutral_run_id
                  AND LOWER(j.status) = 'completed'
                  AND jsonb_array_length({artifact_ids_json}) > 0
            )
            AND EXISTS (
                SELECT 1
                FROM public.calibration_render_jobs j
                WHERE j.run_id = t.control_run_id
                  AND LOWER(j.status) = 'completed'
                  AND jsonb_array_length({artifact_ids_json}) > 0
            )
            AND EXISTS (
                SELECT 1
                FROM public.calibration_render_jobs j
                WHERE j.run_id = t.challenger_run_id
                  AND LOWER(j.status) = 'completed'
                  AND jsonb_array_length({artifact_ids_json}) > 0
            )

            AND EXISTS (
                SELECT 1
                FROM public.audio_artifacts a
                WHERE a.run_id = t.neutral_run_id
                  AND (a.storage_uri LIKE 's3://%' OR a.storage_uri LIKE 'https://%')
                  AND COALESCE(a.sample_pack_version, '') = COALESCE(t.sample_pack_version, '')
                  AND {recipe_json} ->> 'renderer_version' = t.renderer_version
                  AND COALESCE({recipe_json} ->> 'sha256', '') <> ''
                  AND LOWER(COALESCE({recipe_json} ->> 'diagnostic_only', 'false'))
                      NOT IN ('true', '1', 'yes')
                  AND LOWER(COALESCE({recipe_json} ->> 'renderer', ''))
                      NOT LIKE '%procedural%'
            )
            AND EXISTS (
                SELECT 1
                FROM public.audio_artifacts a
                WHERE a.run_id = t.visible_a_run_id
                  AND (a.storage_uri LIKE 's3://%' OR a.storage_uri LIKE 'https://%')
                  AND COALESCE(a.sample_pack_version, '') = COALESCE(t.sample_pack_version, '')
                  AND {recipe_json} ->> 'renderer_version' = t.renderer_version
                  AND COALESCE({recipe_json} ->> 'sha256', '') <> ''
                  AND LOWER(COALESCE({recipe_json} ->> 'diagnostic_only', 'false'))
                      NOT IN ('true', '1', 'yes')
                  AND LOWER(COALESCE({recipe_json} ->> 'renderer', ''))
                      NOT LIKE '%procedural%'
            )
            AND EXISTS (
                SELECT 1
                FROM public.audio_artifacts a
                WHERE a.run_id = t.visible_b_run_id
                  AND (a.storage_uri LIKE 's3://%' OR a.storage_uri LIKE 'https://%')
                  AND COALESCE(a.sample_pack_version, '') = COALESCE(t.sample_pack_version, '')
                  AND {recipe_json} ->> 'renderer_version' = t.renderer_version
                  AND COALESCE({recipe_json} ->> 'sha256', '') <> ''
                  AND LOWER(COALESCE({recipe_json} ->> 'diagnostic_only', 'false'))
                      NOT IN ('true', '1', 'yes')
                  AND LOWER(COALESCE({recipe_json} ->> 'renderer', ''))
                      NOT LIKE '%procedural%'
            )
        """

    def refresh_for_reviewer(self, reviewer_id: str) -> ReadinessRefreshResult:
        reviewer = str(reviewer_id or "").strip()
        if not reviewer:
            raise ValueError("reviewer_id is required")
        predicate = self._strict_predicate()

        with self._engine.begin() as conn:
            invalidated = conn.execute(
                text(
                    f"""
                    UPDATE public.calibration_trials t
                    SET status = 'failed',
                        error_text = COALESCE(
                            NULLIF(t.error_text, ''),
                            'Trial failed strict reviewer-readiness validation'
                        ),
                        updated_at = NOW()
                    FROM public.evaluation_sessions s
                    WHERE t.session_id = s.session_id
                      AND s.reviewer_id = :reviewer_id
                      AND t.status = 'ready'
                      AND NOT ({predicate})
                    """
                ),
                {"reviewer_id": reviewer},
            )
            promoted = conn.execute(
                text(
                    f"""
                    UPDATE public.calibration_trials t
                    SET status = 'ready',
                        error_text = NULL,
                        updated_at = NOW()
                    FROM public.evaluation_sessions s
                    WHERE t.session_id = s.session_id
                      AND s.reviewer_id = :reviewer_id
                      AND t.status = 'queued'
                      AND ({predicate})
                    """
                ),
                {"reviewer_id": reviewer},
            )

        return ReadinessRefreshResult(
            ready_count=max(0, int(promoted.rowcount or 0)),
            invalidated_count=max(0, int(invalidated.rowcount or 0)),
        )

    def diagnostics_for_trial(self, trial_id: str) -> Dict[str, Any]:
        trial = str(trial_id or "").strip()
        if not trial:
            raise ValueError("trial_id is required")
        events_json = self._json_array("e.event_stream_json")
        artifact_ids_json = self._json_array("j.artifact_ids_json")
        recipe_json = self._json_object("a.render_recipe_json")
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"""
                    SELECT
                        t.trial_id,
                        t.status,
                        t.neutral_run_id,
                        t.control_run_id,
                        t.challenger_run_id,
                        t.visible_a_run_id,
                        t.visible_b_run_id,
                        t.renderer_version,
                        t.sample_pack_version,
                        t.generation_metadata_json #>> '{{control,production_metadata,backend}}' AS control_backend,
                        t.generation_metadata_json #>> '{{challenger,production_metadata,backend}}' AS challenger_backend,
                        (
                            SELECT COUNT(*)
                            FROM public.calibration_run_events e
                            WHERE e.run_id IN (
                                t.neutral_run_id,
                                t.control_run_id,
                                t.challenger_run_id
                            )
                              AND jsonb_typeof({events_json}) = 'array'
                              AND jsonb_array_length({events_json}) > 0
                        ) AS populated_event_streams,
                        (
                            SELECT COUNT(*)
                            FROM public.calibration_render_jobs j
                            WHERE j.run_id IN (
                                t.neutral_run_id,
                                t.control_run_id,
                                t.challenger_run_id
                            )
                              AND LOWER(j.status) = 'completed'
                              AND jsonb_array_length({artifact_ids_json}) > 0
                        ) AS completed_jobs,
                        (
                            SELECT COUNT(*)
                            FROM public.audio_artifacts a
                            WHERE a.run_id IN (
                                t.neutral_run_id,
                                t.visible_a_run_id,
                                t.visible_b_run_id
                            )
                              AND (a.storage_uri LIKE 's3://%' OR a.storage_uri LIKE 'https://%')
                              AND COALESCE({recipe_json} ->> 'sha256', '') <> ''
                              AND LOWER(COALESCE({recipe_json} ->> 'diagnostic_only', 'false'))
                                  NOT IN ('true', '1', 'yes')
                        ) AS durable_artifacts,
                        ({self._strict_predicate()}) AS strict_ready
                    FROM public.calibration_trials t
                    WHERE t.trial_id = :trial_id
                    LIMIT 1
                    """
                ),
                {"trial_id": trial},
            ).mappings().first()
        return dict(row) if row else {}

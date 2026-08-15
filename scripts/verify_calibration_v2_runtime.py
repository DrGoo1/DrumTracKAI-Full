from __future__ import annotations

"""Strict runtime evidence collector for Calibration v2.

Run from a Render shell with internal access to the private generation service.
The script never prints credentials, full database URLs, bearer tokens, or
presigned artifact URLs.
"""

import argparse
import json
import os
import time
from typing import Any, Dict, Optional

import requests
from sqlalchemy import create_engine, text


class VerificationError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    value = str(os.getenv(name, "")).strip()
    if not value:
        raise VerificationError(f"{name} is required")
    return value


def _safe_json(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {"non_json": True, "body_prefix": response.text[:240]}
    return payload if isinstance(payload, dict) else {"payload_type": type(payload).__name__}


def _generation_evidence(base_url: str) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    health_response = requests.get(base + "/healthz", timeout=30)
    ready_response = requests.get(base + "/readyz", timeout=30)
    health = _safe_json(health_response)
    ready = _safe_json(ready_response)

    evidence = {
        "health_status": health_response.status_code,
        "ready_status": ready_response.status_code,
        "backend": ready.get("backend") or health.get("backend"),
        "ready": ready.get("ready"),
        "backend_ready": ready.get("backend_ready"),
        "model_version": ready.get("model_version") or health.get("model_version"),
        "model_sha256_verified": ready.get("model_sha256_verified")
        if "model_sha256_verified" in ready
        else health.get("model_sha256_verified"),
        "reason": ready.get("reason") or health.get("reason"),
    }
    if health_response.status_code != 200:
        raise VerificationError("Generation /healthz did not return HTTP 200")
    if ready_response.status_code != 200:
        raise VerificationError(f"Generation /readyz did not return HTTP 200: {evidence}")
    if evidence["ready"] is not True:
        raise VerificationError(f"Generation readiness is not true: {evidence}")
    if str(evidence["backend"] or "").lower() != "onnx":
        raise VerificationError(f"Generation backend is not ONNX: {evidence}")
    if evidence["backend_ready"] is not True:
        raise VerificationError(f"Generation backend_ready is not true: {evidence}")
    if evidence["model_sha256_verified"] is not True:
        raise VerificationError(f"Generation model SHA is not verified: {evidence}")
    return evidence


def _trial_row(conn, trial_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if trial_id:
        row = conn.execute(
            text(
                """
                SELECT *
                FROM public.calibration_trials
                WHERE trial_id = :trial_id
                LIMIT 1
                """
            ),
            {"trial_id": trial_id},
        ).mappings().first()
    else:
        row = conn.execute(
            text(
                """
                SELECT *
                FROM public.calibration_trials
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
    return dict(row) if row else None


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _backend_from_trial(trial: Dict[str, Any], role: str) -> Optional[str]:
    metadata = _json_value(trial.get("generation_metadata_json"), {})
    role_meta = metadata.get(role) if isinstance(metadata, dict) else {}
    production = role_meta.get("production_metadata") if isinstance(role_meta, dict) else {}
    value = production.get("backend") if isinstance(production, dict) else None
    return str(value).strip().lower() if value else None


def _db_evidence(database_url: str, trial_id: Optional[str]) -> Dict[str, Any]:
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    with engine.connect() as conn:
        trial = _trial_row(conn, trial_id)
        if not trial:
            raise VerificationError("Calibration trial was not found")
        selected_trial_id = str(trial["trial_id"])
        run_ids = [
            str(trial.get("neutral_run_id") or ""),
            str(trial.get("control_run_id") or ""),
            str(trial.get("challenger_run_id") or ""),
        ]
        if any(not value for value in run_ids):
            raise VerificationError("Trial is missing one or more run IDs")

        event_rows = conn.execute(
            text(
                """
                SELECT
                    run_id,
                    CASE
                      WHEN event_stream_json IS NULL
                        OR BTRIM(event_stream_json::text) IN ('', 'null')
                      THEN 0
                      ELSE jsonb_array_length(event_stream_json::jsonb)
                    END AS event_count
                FROM public.calibration_run_events
                WHERE run_id IN (:neutral, :control, :challenger)
                ORDER BY run_id
                """
            ),
            {
                "neutral": run_ids[0],
                "control": run_ids[1],
                "challenger": run_ids[2],
            },
        ).mappings().all()

        job_rows = conn.execute(
            text(
                """
                SELECT
                    job_id,
                    run_id,
                    status,
                    CASE
                      WHEN artifact_ids_json IS NULL
                        OR BTRIM(artifact_ids_json::text) IN ('', 'null')
                      THEN 0
                      ELSE jsonb_array_length(artifact_ids_json::jsonb)
                    END AS artifact_id_count,
                    error_text,
                    created_at,
                    started_at,
                    completed_at
                FROM public.calibration_render_jobs
                WHERE run_id IN (:neutral, :control, :challenger)
                ORDER BY created_at
                """
            ),
            {
                "neutral": run_ids[0],
                "control": run_ids[1],
                "challenger": run_ids[2],
            },
        ).mappings().all()

        artifact_rows = conn.execute(
            text(
                """
                SELECT
                    run_id,
                    artifact_id,
                    artifact_type,
                    CASE
                      WHEN storage_uri LIKE 's3://%' THEN 's3'
                      WHEN storage_uri LIKE 'https://%' THEN 'https'
                      ELSE 'non_durable'
                    END AS storage_kind,
                    duration_sec,
                    sample_pack_version,
                    CASE
                      WHEN render_recipe_json IS NULL
                        OR BTRIM(render_recipe_json::text) IN ('', 'null')
                      THEN '{}'::jsonb
                      ELSE render_recipe_json::jsonb
                    END AS recipe
                FROM public.audio_artifacts
                WHERE run_id IN (:neutral, :control, :challenger)
                ORDER BY created_at
                """
            ),
            {
                "neutral": run_ids[0],
                "control": run_ids[1],
                "challenger": run_ids[2],
            },
        ).mappings().all()

        judgment_count = int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM public.pairwise_judgments
                    WHERE item_id = :item_id
                      AND reviewer_id = :reviewer_id
                    """
                ),
                {
                    "item_id": str(trial.get("item_id") or ""),
                    "reviewer_id": str(trial.get("reviewer_id") or ""),
                },
            ).scalar_one()
        )

    event_counts = {str(row["run_id"]): int(row["event_count"] or 0) for row in event_rows}
    jobs = [
        {
            "job_id": str(row["job_id"]),
            "run_id": str(row["run_id"]),
            "status": str(row["status"]),
            "artifact_id_count": int(row["artifact_id_count"] or 0),
            "has_error": bool(str(row.get("error_text") or "").strip()),
            "started": row.get("started_at") is not None,
            "completed": row.get("completed_at") is not None,
        }
        for row in job_rows
    ]
    artifacts = []
    for row in artifact_rows:
        recipe = _json_value(row.get("recipe"), {})
        artifacts.append(
            {
                "run_id": str(row["run_id"]),
                "artifact_id": str(row["artifact_id"]),
                "artifact_type": str(row["artifact_type"]),
                "storage_kind": str(row["storage_kind"]),
                "duration_valid": float(row.get("duration_sec") or 0.0) > 0.0,
                "sample_pack_version": str(row.get("sample_pack_version") or ""),
                "renderer": str(recipe.get("renderer") or ""),
                "renderer_version": str(recipe.get("renderer_version") or ""),
                "sha256_present": bool(str(recipe.get("sha256") or "").strip()),
                "diagnostic_only": bool(recipe.get("diagnostic_only")),
            }
        )

    evidence = {
        "trial_id": selected_trial_id,
        "item_id": str(trial.get("item_id") or ""),
        "reviewer_id": str(trial.get("reviewer_id") or ""),
        "trial_status": str(trial.get("status") or ""),
        "control_backend": _backend_from_trial(trial, "control"),
        "challenger_backend": _backend_from_trial(trial, "challenger"),
        "renderer_version": str(trial.get("renderer_version") or ""),
        "sample_pack_version": str(trial.get("sample_pack_version") or ""),
        "event_counts": event_counts,
        "jobs": jobs,
        "artifacts": artifacts,
        "judgment_count": judgment_count,
    }

    if evidence["control_backend"] != "onnx" or evidence["challenger_backend"] != "onnx":
        raise VerificationError(f"Trial generation backend is not ONNX: {evidence}")
    if len(event_counts) != 3 or any(count <= 0 for count in event_counts.values()):
        raise VerificationError(f"Trial does not have three non-empty event streams: {evidence}")
    if len(jobs) != 3:
        raise VerificationError(f"Trial does not have exactly three render jobs: {evidence}")
    if any(job["status"].lower() != "completed" or job["artifact_id_count"] <= 0 for job in jobs):
        raise VerificationError(f"Trial render jobs are not all complete: {evidence}")
    if len(artifacts) < 3:
        raise VerificationError(f"Trial has fewer than three durable artifacts: {evidence}")
    if any(
        artifact["storage_kind"] not in {"s3", "https"}
        or not artifact["duration_valid"]
        or not artifact["sha256_present"]
        or artifact["diagnostic_only"]
        or "procedural" in artifact["renderer"].lower()
        for artifact in artifacts
    ):
        raise VerificationError(f"Trial contains invalid or diagnostic artifacts: {evidence}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify strict Calibration v2 runtime")
    parser.add_argument("--trial-id", default="")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()

    try:
        generation_base = _required_env("CALIBRATION_GENERATION_API_BASE")
        database_url = _required_env("DATABASE_URL")
        generation = _generation_evidence(generation_base)

        deadline = time.monotonic() + max(0, int(args.wait_seconds))
        last_error: Optional[Exception] = None
        while True:
            try:
                trial = _db_evidence(database_url, str(args.trial_id or "").strip() or None)
                output = {"ok": True, "generation": generation, "trial": trial}
                print(json.dumps(output, indent=2, default=str, sort_keys=True))
                print("CALIBRATION_V2_STRICT_RUNTIME_PASS")
                return 0
            except Exception as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise
                time.sleep(max(0.5, float(args.poll_seconds)))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

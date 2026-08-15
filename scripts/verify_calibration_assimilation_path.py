from __future__ import annotations

"""Verify signature-song assimilation is driving Calibration v2 generation.

Run this inside API2 or another environment with access to the canonical
Postgres database and the private generation service.  The report contains
counts and immutable hashes only; it never prints source-song URLs, profile
payloads, database credentials, tokens, or hidden A/B assignment data.
"""

import argparse
import json
import os
from typing import Any, Dict

from admin.services.central_database_service import CentralDatabaseService
from backend.services.calibration_assimilation_service import CalibrationAssimilationService
from backend.services.calibration_production_engine import (
    CalibrationProductionEngine,
    validate_treatment_overrides,
)
from backend.services.calibration_v2_repository import CalibrationV2Repository


class AssimilationVerificationError(RuntimeError):
    pass


def _backend(candidate: Any) -> str:
    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    production = metadata.get("production_metadata") if isinstance(metadata.get("production_metadata"), dict) else {}
    return str(production.get("backend") or "").strip().lower()


def _model_version(candidate: Any) -> str:
    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    production = metadata.get("production_metadata") if isinstance(metadata.get("production_metadata"), dict) else {}
    return str(production.get("model_version") or production.get("version") or "unknown")


def _probe_generation(
    *,
    db: CentralDatabaseService,
    repo: CalibrationV2Repository,
    drummer_slug: str,
    treatment_id: str,
    base_groove_id: str,
    seed: int,
    repeats: int,
) -> Dict[str, Any]:
    treatment = repo.get_treatment(treatment_id)
    normalized = validate_treatment_overrides(
        cfg_overrides=treatment.get("cfg_overrides") or {},
        profile_overrides=treatment.get("profile_overrides") or {},
    )
    engine = CalibrationProductionEngine(db)
    control = engine.generate_candidate(
        role="control",
        base_groove_id=base_groove_id,
        drummer_slug=drummer_slug,
        seed=seed,
        repeats=repeats,
        cfg_overrides={},
        profile_overrides={},
        treatment_id=None,
        kit_id="assimilation_verification",
    )
    challenger = engine.generate_candidate(
        role="challenger",
        base_groove_id=base_groove_id,
        drummer_slug=drummer_slug,
        seed=seed,
        repeats=repeats,
        cfg_overrides=normalized["cfg_overrides"],
        profile_overrides=normalized["profile_overrides"],
        treatment_id=treatment_id,
        kit_id="assimilation_verification",
    )

    evidence = {
        "production_endpoint": control.metadata.get("production_endpoint"),
        "control_backend": _backend(control),
        "challenger_backend": _backend(challenger),
        "control_model_version": _model_version(control),
        "challenger_model_version": _model_version(challenger),
        "control_profile_snapshot_hash": control.metadata.get("profile_snapshot_hash"),
        "challenger_profile_snapshot_hash": challenger.metadata.get("profile_snapshot_hash"),
        "control_rollup_version": control.metadata.get("rollup_version"),
        "challenger_rollup_version": challenger.metadata.get("rollup_version"),
        "base_pattern_hash": control.metadata.get("base_pattern_hash"),
        "paired_seed": control.metadata.get("paired_seed"),
        "control_event_stream_hash": control.metadata.get("event_stream_hash"),
        "challenger_event_stream_hash": challenger.metadata.get("event_stream_hash"),
        "control_event_count": len(control.event_stream),
        "challenger_event_count": len(challenger.event_stream),
        "treatment_id": treatment_id,
    }

    if evidence["control_backend"] != "onnx" or evidence["challenger_backend"] != "onnx":
        raise AssimilationVerificationError(
            f"Canonical generation backend is not ONNX: {evidence['control_backend']}/{evidence['challenger_backend']}"
        )
    if not evidence["control_profile_snapshot_hash"] or not evidence["challenger_profile_snapshot_hash"]:
        raise AssimilationVerificationError("Generated candidates do not contain profile snapshot hashes")
    if evidence["control_profile_snapshot_hash"] == evidence["challenger_profile_snapshot_hash"]:
        # A cfg-only Phase 6 treatment may intentionally retain the same profile hash.
        if not normalized["cfg_overrides"]:
            raise AssimilationVerificationError("Control and challenger are not distinguished by profile or cfg treatment")
    if control.metadata.get("base_pattern_hash") != challenger.metadata.get("base_pattern_hash"):
        raise AssimilationVerificationError("Control and challenger use different base patterns")
    if control.metadata.get("paired_seed") != challenger.metadata.get("paired_seed"):
        raise AssimilationVerificationError("Control and challenger use different paired seeds")
    if evidence["control_event_stream_hash"] == evidence["challenger_event_stream_hash"]:
        raise AssimilationVerificationError("Treatment produced no event-stream difference")
    if evidence["control_event_count"] <= 0 or evidence["challenger_event_count"] <= 0:
        raise AssimilationVerificationError("Generated event stream is empty")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Admin assimilation -> Calibration v2 path")
    parser.add_argument("--drummer", required=True, help="Canonical drummer slug")
    parser.add_argument("--probe-generation", action="store_true")
    parser.add_argument("--bootstrap-treatment", action="store_true")
    parser.add_argument("--created-by", default="", help="Admin Supabase UUID for treatment creation")
    parser.add_argument(
        "--base-groove-id",
        default=os.getenv("CALIBRATION_DEFAULT_BASE_GROOVE_ID", "base_groove"),
    )
    parser.add_argument("--seed", type=int, default=130742)
    parser.add_argument("--repeats", type=int, default=4)
    args = parser.parse_args()

    try:
        db = CentralDatabaseService.get_instance()
        if not db.initialize():
            raise AssimilationVerificationError("Central database initialization failed")
        repo = CalibrationV2Repository(db)
        service = CalibrationAssimilationService(db, repository=repo)

        if args.bootstrap_treatment:
            created_by = str(args.created_by or "").strip()
            if not created_by:
                raise AssimilationVerificationError("--created-by is required with --bootstrap-treatment")
            service.bootstrap_phase6_treatment(
                drummer_slug=args.drummer,
                created_by=created_by,
            )

        status = service.model_status(args.drummer)
        report: Dict[str, Any] = {
            "drummer_slug": status.drummer_slug,
            "display_name": status.display_name,
            "profile_resolved": status.profile_resolved,
            "model_ready": status.model_ready,
            "can_queue_trial": status.can_queue_trial,
            "rollup_version": status.rollup_version,
            "profile_snapshot_hash": status.profile_snapshot_hash,
            "source_counts": status.source_counts,
            "source_song_count": status.source_song_count,
            "source_stem_count": status.source_stem_count,
            "hit_event_count": status.hit_event_count,
            "fill_event_count": status.fill_event_count,
            "technique_event_count": status.technique_event_count,
            "assimilation_score": status.assimilation_score,
            "profile_sections": status.profile_sections,
            "active_treatment_id": status.active_treatment_id,
            "blockers": status.blockers,
        }
        if not status.model_ready:
            raise AssimilationVerificationError(
                "Assimilated model is not calibration-ready: " + "; ".join(status.blockers)
            )

        if args.probe_generation:
            if not status.active_treatment_id:
                raise AssimilationVerificationError(
                    "No active treatment exists; run Admin Phase 6 treatment bootstrap first"
                )
            report["generation_probe"] = _probe_generation(
                db=db,
                repo=repo,
                drummer_slug=status.drummer_slug,
                treatment_id=status.active_treatment_id,
                base_groove_id=str(args.base_groove_id),
                seed=int(args.seed),
                repeats=max(1, int(args.repeats)),
            )

        print(json.dumps({"ok": True, "report": report}, indent=2, sort_keys=True, default=str))
        print("CALIBRATION_ASSIMILATION_PATH_PASS")
        return 0
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

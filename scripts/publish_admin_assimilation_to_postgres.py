from __future__ import annotations

"""Publish locally extracted Admin-app assimilation data to canonical Postgres.

The Admin app performs source acquisition, stem separation, hit extraction and
feature analysis on the workstation where the processed stems exist.  Render
cannot read those local paths.  This command runs the existing ingestion and
Phase 2-7/32-42 pipeline locally while directing CentralDatabaseService writes
to the same Postgres database used by Calibration v2.

Security:
- Prefer DATABASE_URL from the process environment; it is never printed.
- The script prints only a short database fingerprint and aggregate evidence.
- Source media, YouTube URLs and local absolute paths are not included in the
  final JSON report.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


class AssimilationPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class SongFolder:
    drummer_slug: str
    path: Path


PHASE_METHODS: Sequence[tuple[str, str]] = (
    ("phase2", "run_phase2_hit_event_extraction_for_drummer"),
    ("phase3", "run_phase3_fills_and_techniques_for_drummer"),
    ("phase4", "run_phase4_microtiming_and_dynamics_for_drummer"),
    ("phase5", "run_phase5_profile_rollup_for_drummer"),
    ("phase6", "run_phase6_persona_preset_export_for_drummer"),
    ("phase7", "run_phase7_assimilation_profiles_for_drummer"),
    ("phase32_42", "run_phase32_42_features_for_drummer"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_fingerprint(database_url: str) -> str:
    return hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:16]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def discover_song_folders(
    base_dir: Path,
    *,
    requested_slugs: Optional[Iterable[str]] = None,
) -> List[SongFolder]:
    root = base_dir.expanduser().resolve()
    if not root.is_dir():
        raise AssimilationPublishError(f"Processed-stems directory does not exist: {root}")

    requested = {
        str(value or "").strip()
        for value in (requested_slugs or [])
        if str(value or "").strip()
    }
    discovered: List[SongFolder] = []
    for slug_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        slug = slug_dir.name.strip()
        if not slug or (requested and slug not in requested):
            continue
        for song_dir in sorted(path for path in slug_dir.iterdir() if path.is_dir()):
            if (song_dir / "drum_analysis.json").is_file():
                discovered.append(SongFolder(drummer_slug=slug, path=song_dir.resolve()))

    if requested:
        found = {item.drummer_slug for item in discovered}
        missing = sorted(requested - found)
        if missing:
            raise AssimilationPublishError(
                "No processed song folders with drum_analysis.json were found for: "
                + ", ".join(missing)
            )
    if not discovered:
        raise AssimilationPublishError(
            f"No <drummer>/<song>/drum_analysis.json folders were found under {root}"
        )
    return discovered


def _phase_failure(phase_name: str, result: Any) -> Optional[str]:
    if result is None:
        return f"{phase_name} returned no result"
    if not isinstance(result, dict):
        return None
    error = str(result.get("error") or "").strip()
    if error:
        return error
    if "saved" in result and not bool(result.get("saved")):
        phase7 = result.get("phase7") if isinstance(result.get("phase7"), dict) else {}
        nested = str(phase7.get("error") or "").strip()
        return nested or "saved=false"
    return None


def _safe_phase_summary(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    safe_keys = (
        "saved",
        "processed",
        "created",
        "inserted",
        "updated",
        "events",
        "hit_events",
        "fill_events",
        "technique_events",
        "analysis_count",
        "rollup_version",
        "preset_id",
        "profile_version",
    )
    summary: Dict[str, Any] = {}
    for key in safe_keys:
        value = result.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if key in result:
                summary[key] = value
    if result.get("error"):
        summary["error"] = str(result.get("error"))
    return summary


def _initialize_postgres_service():
    database_url = str(os.getenv("DATABASE_URL", "")).strip()
    if not database_url.lower().startswith(("postgres://", "postgresql://", "postgresql+")):
        raise AssimilationPublishError(
            "DATABASE_URL must be set to the canonical Calibration v2 Postgres database"
        )
    os.environ["DB_BACKEND"] = "postgres"

    # Import only after DB_BACKEND/DATABASE_URL have been finalized.
    from admin.services.central_database_service import CentralDatabaseService

    db = CentralDatabaseService.get_instance()
    if db is None or not db.initialize():
        raise AssimilationPublishError("CentralDatabaseService could not initialize Postgres")
    engine = getattr(db, "_engine", None)
    if engine is None or str(getattr(engine.dialect, "name", "")).lower() != "postgresql":
        raise AssimilationPublishError("CentralDatabaseService did not initialize a PostgreSQL engine")
    return db, database_url


def publish_drummer(
    *,
    db: Any,
    drummer_slug: str,
    song_folders: Sequence[Path],
    max_events_per_stem: int,
    compute_hashes: bool,
    hash_max_bytes: int,
    skip_ingest: bool,
) -> Dict[str, Any]:
    slug = str(drummer_slug or "").strip()
    if not slug:
        raise AssimilationPublishError("drummer_slug is required")

    ingested_analysis_ids: List[str] = []
    ingest_failures: List[Dict[str, str]] = []
    if not skip_ingest:
        for song_dir in song_folders:
            try:
                analysis_id = db.ingest_processed_stems_song_folder(
                    drummer_id=slug,
                    song_folder=str(song_dir),
                    compute_hashes=bool(compute_hashes),
                    hash_max_bytes=max(0, int(hash_max_bytes)),
                    analysis_version="calibration_cloud_v2",
                )
                if analysis_id:
                    ingested_analysis_ids.append(str(analysis_id))
                else:
                    ingest_failures.append(
                        {"song_folder": song_dir.name, "error": "ingest returned no analysis_id"}
                    )
            except Exception as exc:
                ingest_failures.append({"song_folder": song_dir.name, "error": str(exc)})

    if ingest_failures:
        raise AssimilationPublishError(
            f"{slug}: {len(ingest_failures)} processed song folders failed ingestion; "
            f"first failure={ingest_failures[0]['song_folder']}: {ingest_failures[0]['error']}"
        )

    phase_reports: Dict[str, Dict[str, Any]] = {}
    for phase_name, method_name in PHASE_METHODS:
        method = getattr(db, method_name, None)
        if not callable(method):
            raise AssimilationPublishError(f"CentralDatabaseService lacks {method_name}")
        kwargs: Dict[str, Any] = {"drummer_slug": slug}
        if phase_name == "phase2":
            kwargs["max_events_per_stem"] = max(1, int(max_events_per_stem))
        result = method(**kwargs)
        phase_reports[phase_name] = _safe_phase_summary(result)
        failure = _phase_failure(phase_name, result)
        if failure:
            raise AssimilationPublishError(f"{slug}: {phase_name} failed: {failure}")

    from backend.services.calibration_assimilation_service import CalibrationAssimilationService
    from backend.services.calibration_profile_resolver import CalibrationProfileResolver
    from backend.services.calibration_v2_repository import CalibrationV2Repository

    resolver = CalibrationProfileResolver(db)
    resolved = resolver.resolve(drummer_slug=slug, strict=True)
    catalog = CalibrationAssimilationService(
        db,
        repository=CalibrationV2Repository(db),
        resolver=resolver,
    )
    model_status = catalog.model_status(slug)
    if not model_status.model_ready:
        raise AssimilationPublishError(
            f"{slug}: imported assimilation did not become calibration-ready: "
            + "; ".join(model_status.blockers)
        )

    return {
        "drummer_slug": slug,
        "song_folder_count": len(song_folders),
        "ingested_analysis_count": len(ingested_analysis_ids),
        "ingested_analysis_ids_sha256": hashlib.sha256(
            json.dumps(sorted(ingested_analysis_ids)).encode("utf-8")
        ).hexdigest(),
        "phase_reports": phase_reports,
        "rollup_version": resolved.rollup_version,
        "profile_snapshot_hash": resolved.snapshot_hash,
        "profile_source_counts": resolved.source_counts,
        "source_song_count": model_status.source_song_count,
        "source_stem_count": model_status.source_stem_count,
        "hit_event_count": model_status.hit_event_count,
        "fill_event_count": model_status.fill_event_count,
        "technique_event_count": model_status.technique_event_count,
        "assimilation_score": model_status.assimilation_score,
        "profile_sections": model_status.profile_sections,
        "active_treatment_id": model_status.active_treatment_id,
        "model_ready": model_status.model_ready,
        "can_queue_trial": model_status.can_queue_trial,
        "blockers": model_status.blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish local DrumTrackAI Admin assimilation into Calibration v2 Postgres"
    )
    parser.add_argument(
        "--base-dir",
        required=True,
        help="Local root containing <drummer>/<song>/drum_analysis.json folders",
    )
    parser.add_argument(
        "--drummer",
        action="append",
        default=[],
        help="Drummer slug to publish; repeat for multiple. Omit to publish every discovered slug.",
    )
    parser.add_argument("--max-events-per-stem", type=int, default=5000)
    parser.add_argument("--compute-hashes", action="store_true")
    parser.add_argument("--hash-max-bytes", type=int, default=0)
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Re-run feature/profile phases against rows already present in Postgres",
    )
    parser.add_argument(
        "--report",
        default="",
        help="Optional local JSON report path; secrets and source paths are omitted",
    )
    args = parser.parse_args()

    try:
        db, database_url = _initialize_postgres_service()
        folders = discover_song_folders(
            Path(args.base_dir),
            requested_slugs=args.drummer,
        )
        grouped: Dict[str, List[Path]] = {}
        for item in folders:
            grouped.setdefault(item.drummer_slug, []).append(item.path)

        reports: List[Dict[str, Any]] = []
        for slug in sorted(grouped):
            reports.append(
                publish_drummer(
                    db=db,
                    drummer_slug=slug,
                    song_folders=grouped[slug],
                    max_events_per_stem=max(1, int(args.max_events_per_stem)),
                    compute_hashes=bool(args.compute_hashes),
                    hash_max_bytes=max(0, int(args.hash_max_bytes)),
                    skip_ingest=bool(args.skip_ingest),
                )
            )

        output = {
            "ok": True,
            "published_at": _utc_now(),
            "database_fingerprint": _db_fingerprint(database_url),
            "drummer_count": len(reports),
            "reports": reports,
        }
        encoded = json.dumps(output, indent=2, sort_keys=True, default=str)
        if str(args.report or "").strip():
            report_path = Path(args.report).expanduser().resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(encoded + "\n", encoding="utf-8")
        print(encoded)
        print("ADMIN_ASSIMILATION_PUBLISH_PASS")
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

"""Resolve an immutable production drummer profile snapshot from Postgres.

The calibration system must never read the legacy SQLite profile registry in
production.  This resolver assembles the same profile categories consumed by
``llm_service.app.performance_spec`` and records a deterministic snapshot hash.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from sqlalchemy import text

from admin.services.central_database_service import CentralDatabaseService


class CalibrationProfileUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedCalibrationProfile:
    drummer_slug: str
    profile: Dict[str, Any]
    snapshot_hash: str
    rollup_version: Optional[str]
    source_counts: Dict[str, int]


def _json_load(value: Any, default: Any) -> Any:
    if value is None:
        return deepcopy(default)
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(str(value))
    except Exception:
        return deepcopy(default)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _mean(values: Iterable[Any]) -> Optional[float]:
    parsed = [value for value in (_safe_float(item) for item in values) if value is not None]
    return float(sum(parsed) / len(parsed)) if parsed else None


def _aggregate_phase32_42(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate the currently stored Phase 32-42 payload shape.

    This mirrors the useful behavior of the legacy sentient profile registry but
    operates solely on rows already fetched from Postgres.
    """
    phase_rows = [
        payload.get("phase37_42")
        for payload in payloads
        if isinstance(payload, dict) and isinstance(payload.get("phase37_42"), dict)
    ]
    if not phase_rows:
        return {}

    personality_fields = [
        "aggressiveness",
        "restraint",
        "consistency",
        "chaos",
        "ghostStyle",
        "kickDrive",
    ]
    personalities = [
        row.get("drummer_personality_profile")
        for row in phase_rows
        if isinstance(row.get("drummer_personality_profile"), dict)
    ]
    personality: Dict[str, Any] = {
        key: _mean(item.get(key) for item in personalities) for key in personality_fields
    }
    personality = {key: value for key, value in personality.items() if value is not None}
    signature_habits = {
        "crashBias": _mean(
            (item.get("signatureHabits") or {}).get("crashBias")
            for item in personalities
            if isinstance(item.get("signatureHabits"), dict)
        ),
        "accentBias": _mean(
            (item.get("signatureHabits") or {}).get("accentBias")
            for item in personalities
            if isinstance(item.get("signatureHabits"), dict)
        ),
    }
    signature_habits = {key: value for key, value in signature_habits.items() if value is not None}
    if signature_habits:
        personality["signatureHabits"] = signature_habits

    micro_globals = []
    for row in phase_rows:
        micro = row.get("microtiming_profile")
        global_row = micro.get("global") if isinstance(micro, dict) else None
        if isinstance(global_row, dict):
            micro_globals.append(global_row)
    micro_global = {
        "mean_ms": _mean(item.get("mean_ms") for item in micro_globals),
        "std_ms": _mean(item.get("std_ms") for item in micro_globals),
    }
    micro_global = {key: value for key, value in micro_global.items() if value is not None}

    continuity: List[Dict[str, Any]] = []
    for row in phase_rows:
        memory = row.get("phrase_continuity_memory")
        if isinstance(memory, list):
            continuity.extend(item for item in memory if isinstance(item, dict))
    continuity = continuity[-24:]

    def first_dict(key: str) -> Dict[str, Any]:
        for row in phase_rows:
            value = row.get(key)
            if isinstance(value, dict) and value:
                return deepcopy(value)
        return {}

    return {
        "phase37_42": {
            "microtiming_profile": {"global": micro_global} if micro_global else {},
            "limb_interaction_profile": first_dict("limb_interaction_profile"),
            "dynamic_contour_profile": first_dict("dynamic_contour_profile"),
            "phrase_continuity_memory": continuity,
            "drummer_personality_profile": personality,
        }
    }


_PROFILE_NUMERIC_RANGES: Dict[str, tuple[float, float]] = {
    "timing_tightness": (0.0, 1.0),
    "timing_precision": (0.0, 1.0),
    "ghost_note_frequency": (0.0, 1.0),
    "ghost_frequency": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.aggressiveness": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.restraint": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.consistency": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.chaos": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.ghostStyle": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.kickDrive": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.signatureHabits.crashBias": (0.0, 1.0),
    "phase32_42_features.phase37_42.drummer_personality_profile.signatureHabits.accentBias": (0.0, 1.0),
    "phase32_42_features.phase37_42.microtiming_profile.global.mean_ms": (-50.0, 50.0),
    "phase32_42_features.phase37_42.microtiming_profile.global.std_ms": (0.0, 50.0),
}
_PROFILE_STRING_ENUMS: Dict[str, set[str]] = {
    "preferred_feel": {"straight", "swing", "shuffle", "laid_back", "pushed"},
    "feel": {"straight", "swing", "shuffle", "laid_back", "pushed"},
}
_PROFILE_FREE_STRINGS = {"primary_style", "style"}
_PROFILE_STRING_LISTS = {"signature_techniques"}


def _iter_override_leaves(value: Mapping[str, Any], prefix: str = ""):
    for raw_key, item in value.items():
        key = str(raw_key).strip()
        if not key or key.startswith("_"):
            raise ValueError(f"Profile override key is reserved or empty: {raw_key}")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            if not item:
                raise ValueError(f"Profile override object is empty: {path}")
            yield from _iter_override_leaves(item, path)
        else:
            yield path, item


def validate_profile_overrides(overrides: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate a bounded treatment delta without permitting identity/provenance edits.

    Calibration treatments are hypotheses, not arbitrary profile replacements.  The
    allowlist is deliberately restricted to fields consumed by the production
    performance-spec path.  Extend this list only with a migration and tests.
    """
    if not overrides:
        return {}
    if not isinstance(overrides, Mapping):
        raise ValueError("profile_overrides must be an object")

    encoded = json.dumps(overrides, default=str)
    if len(encoded.encode("utf-8")) > 64_000:
        raise ValueError("Profile override payload exceeds 64 KB")

    for path, value in _iter_override_leaves(overrides):
        if path in _PROFILE_NUMERIC_RANGES:
            lo, hi = _PROFILE_NUMERIC_RANGES[path]
            try:
                numeric = float(value)
            except Exception as exc:
                raise ValueError(f"Profile override {path} must be numeric") from exc
            if not lo <= numeric <= hi:
                raise ValueError(f"Profile override {path} must be between {lo} and {hi}")
        elif path in _PROFILE_STRING_ENUMS:
            normalized = str(value).strip().lower()
            if normalized not in _PROFILE_STRING_ENUMS[path]:
                raise ValueError(
                    f"Profile override {path} must be one of {sorted(_PROFILE_STRING_ENUMS[path])}"
                )
        elif path in _PROFILE_FREE_STRINGS:
            if not str(value).strip() or len(str(value)) > 100:
                raise ValueError(f"Profile override {path} must be a non-empty short string")
        elif path in _PROFILE_STRING_LISTS:
            if not isinstance(value, list) or len(value) > 32:
                raise ValueError(f"Profile override {path} must be a list of at most 32 strings")
            if any(not str(item).strip() or len(str(item)) > 120 for item in value):
                raise ValueError(f"Profile override {path} contains an invalid string")
        else:
            raise ValueError(f"Treatment may not override profile path '{path}'")
    return deepcopy(dict(overrides))


def _deep_merge(base: Dict[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    output = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(output.get(key), dict):
            output[key] = _deep_merge(output[key], value)
        else:
            output[key] = deepcopy(value)
    return output


class CalibrationProfileResolver:
    def __init__(self, db: CentralDatabaseService) -> None:
        self._db = db

    def resolve(
        self,
        *,
        drummer_slug: str,
        profile_overrides: Optional[Dict[str, Any]] = None,
        strict: bool = True,
    ) -> ResolvedCalibrationProfile:
        slug = str(drummer_slug or "").strip()
        if not slug:
            raise ValueError("drummer_slug is required")
        engine = getattr(self._db, "_engine", None)
        if engine is None:
            raise CalibrationProfileUnavailable("Calibration profile resolution requires Postgres")

        drummer_keys = [slug]
        try:
            record = self._db.get_drummer(slug)
        except Exception:
            record = None
        if isinstance(record, dict):
            for key in ("id", "drummer_id", "slug", "persona_id"):
                value = str(record.get(key) or "").strip()
                if value and value not in drummer_keys:
                    drummer_keys.append(value)
        drummer_params = {f"drummer_key_{index}": value for index, value in enumerate(drummer_keys)}
        drummer_predicate = " OR ".join(
            f"CAST(drummer_id AS TEXT) = :drummer_key_{index}"
            for index in range(len(drummer_keys))
        )

        with engine.connect() as conn:
            rollup_row = conn.execute(
                text(
                    f"""
                    SELECT rollup_version, rollup_json
                    FROM public.drummer_profile_rollups
                    WHERE ({drummer_predicate})
                    LIMIT 1
                    """
                ),
                drummer_params,
            ).mappings().first()
            if not rollup_row:
                raise CalibrationProfileUnavailable(f"No saved assimilation rollup for '{slug}'")

            timing_rows = conn.execute(
                text(
                    f"""
                    SELECT instrument, subdivision, mean_offset_ms, std_offset_ms,
                           skew_offset_ms, early_hit_probability, late_hit_probability,
                           pocket_bias, context_label, histogram_json
                    FROM public.drummer_microtiming_profiles
                    WHERE ({drummer_predicate})
                    ORDER BY instrument, context_label, subdivision
                    """
                ),
                drummer_params,
            ).mappings().all()
            dynamic_rows = conn.execute(
                text(
                    f"""
                    SELECT instrument, velocity_mean, velocity_std, velocity_skew,
                           ghost_note_probability, accent_probability,
                           ghost_to_accent_ratio, accent_grid_json,
                           velocity_histogram_json, phrase_dynamic_curve_json
                    FROM public.drummer_dynamic_profiles
                    WHERE ({drummer_predicate})
                    ORDER BY instrument
                    """
                ),
                drummer_params,
            ).mappings().all()
            fill_rows = conn.execute(
                text(
                    f"""
                    SELECT section_label, phrase_position, fill_probability,
                           fill_length_mean_beats, fill_length_std_beats,
                           fill_density_mean, tom_usage_probability,
                           snare_fill_probability, kick_fill_probability,
                           cymbal_exit_probability, triplet_fill_probability,
                           linear_fill_probability, rudimental_fill_probability,
                           common_fill_shapes_json
                    FROM public.drummer_fill_behavior
                    WHERE ({drummer_predicate})
                    ORDER BY section_label, phrase_position
                    """
                ),
                drummer_params,
            ).mappings().all()
            cymbal_row = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM public.drummer_cymbal_language
                    WHERE ({drummer_predicate})
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                drummer_params,
            ).mappings().first()
            limb_row = conn.execute(
                text(
                    f"""
                    SELECT *
                    FROM public.drummer_limb_coordination
                    WHERE ({drummer_predicate})
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                drummer_params,
            ).mappings().first()
            phrase_rows = conn.execute(
                text(
                    f"""
                    SELECT section_label, phrase_index, phrase_length_bars,
                           bar_position_in_phrase, energy_start, energy_end,
                           energy_slope, pattern_repetition_score,
                           pattern_mutation_rate, density_curve_json,
                           accent_curve_json
                    FROM public.drummer_phrase_features
                    WHERE ({drummer_predicate})
                    ORDER BY created_at DESC
                    LIMIT 256
                    """
                ),
                drummer_params,
            ).mappings().all()
            phase_rows = conn.execute(
                text(
                    f"""
                    SELECT phase32_42_features_json
                    FROM public.song_performance_analysis
                    WHERE ({drummer_predicate})
                      AND phase32_42_features_json IS NOT NULL
                      AND BTRIM(phase32_42_features_json) <> ''
                    ORDER BY created_at DESC
                    LIMIT 128
                    """
                ),
                drummer_params,
            ).all()

        rollup = _json_load(rollup_row.get("rollup_json"), {})
        profile: Dict[str, Any] = dict(rollup if isinstance(rollup, dict) else {})
        profile.setdefault("drummer_id", slug)
        profile.setdefault("publicDrummerId", slug)
        profile.setdefault("source", "postgres_calibration_profile_v2")
        profile.setdefault("profile_version", str(rollup_row.get("rollup_version") or "unknown"))

        timing_payloads = []
        for row in timing_rows:
            item = dict(row)
            item["histogram"] = _json_load(item.pop("histogram_json", None), {})
            item["sample_count"] = int(
                sum((item["histogram"] or {}).values())
                if isinstance(item["histogram"], dict)
                else 0
            )
            timing_payloads.append(item)
        dynamic_payloads = []
        for row in dynamic_rows:
            item = dict(row)
            item["accent_grid"] = _json_load(item.pop("accent_grid_json", None), {})
            item["velocity_histogram"] = _json_load(item.pop("velocity_histogram_json", None), {})
            item["phrase_dynamic_curve"] = _json_load(item.pop("phrase_dynamic_curve_json", None), [])
            item["sample_count"] = int(
                sum((item["velocity_histogram"] or {}).values())
                if isinstance(item["velocity_histogram"], dict)
                else 0
            )
            dynamic_payloads.append(item)
        fill_payloads = []
        for row in fill_rows:
            item = dict(row)
            item["common_fill_shapes"] = _json_load(item.pop("common_fill_shapes_json", None), [])
            fill_payloads.append(item)
        phrase_payloads = []
        for row in phrase_rows:
            item = dict(row)
            item["density_curve"] = _json_load(item.pop("density_curve_json", None), [])
            item["accent_curve"] = _json_load(item.pop("accent_curve_json", None), [])
            phrase_payloads.append(item)

        profile["instrument_timing_profiles"] = timing_payloads
        profile["instrument_dynamic_profiles"] = dynamic_payloads
        profile["fill_behavior"] = fill_payloads
        profile["phrase_features"] = phrase_payloads
        if cymbal_row:
            cymbal = dict(cymbal_row)
            cymbal.pop("id", None)
            cymbal.pop("drummer_id", None)
            cymbal.pop("created_at", None)
            cymbal["cymbal_density_curve"] = _json_load(cymbal.pop("cymbal_density_curve_json", None), [])
            profile["cymbal_language"] = cymbal
        if limb_row:
            limb = dict(limb_row)
            limb.pop("id", None)
            limb.pop("drummer_id", None)
            limb.pop("created_at", None)
            for key in ("simultaneous_hit_matrix_json", "common_limb_patterns_json"):
                parsed_key = key.removesuffix("_json")
                limb[parsed_key] = _json_load(limb.pop(key, None), {})
            profile["limb_coordination"] = limb

        phase_payloads = [_json_load(row[0], {}) for row in phase_rows]
        phase_payloads = [item for item in phase_payloads if isinstance(item, dict) and item]
        phase_aggregate = _aggregate_phase32_42(phase_payloads)
        if phase_aggregate:
            profile["phase32_42_features"] = phase_aggregate

        if profile_overrides:
            profile = _deep_merge(profile, validate_profile_overrides(profile_overrides))

        source_counts = {
            "timing_profiles": len(timing_payloads),
            "dynamic_profiles": len(dynamic_payloads),
            "fill_profiles": len(fill_payloads),
            "phrase_features": len(phrase_payloads),
            "phase32_42_payloads": len(phase_payloads),
        }
        profile["_calibration_provenance"] = {
            "drummer_slug": slug,
            "rollup_version": rollup_row.get("rollup_version"),
            "source_counts": source_counts,
        }

        if strict and not (
            timing_payloads or dynamic_payloads or phase_aggregate or profile.get("timing_tightness") is not None
        ):
            raise CalibrationProfileUnavailable(
                f"Assimilation profile for '{slug}' has a rollup but no production personality features"
            )

        return ResolvedCalibrationProfile(
            drummer_slug=slug,
            profile=profile,
            snapshot_hash=_canonical_hash(profile),
            rollup_version=(str(rollup_row.get("rollup_version")) if rollup_row.get("rollup_version") else None),
            source_counts=source_counts,
        )

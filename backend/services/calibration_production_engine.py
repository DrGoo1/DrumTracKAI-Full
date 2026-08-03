"""Controlled calibration generation using the production performance-spec path.

The base pattern defines *what* is played.  The production performance-spec and
DCSM layers define *how* it is played.  Control and challenger candidates use
identical base events, paired random seed, renderer, and sample pack.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from admin.services.central_database_service import CentralDatabaseService
from backend.dcsmpiano.drumtrack_builder_dcsmpiano import build_drumtrack_for_dcsm
from backend.dcsmpiano.drumtrack_schema import instrument_id_to_midi_pitch
from backend.services.calibration_profile_resolver import (
    CalibrationProfileResolver,
    ResolvedCalibrationProfile,
    validate_profile_overrides,
)
from backend.services.production_performance_client import ProductionPerformanceClient


@dataclass(frozen=True)
class PatternAsset:
    tempo_bpm: float
    time_signature: Tuple[int, int]
    ppqn: int
    events: List[Dict[str, Any]]
    source_path: str


@dataclass(frozen=True)
class _Bar:
    start_time: float
    end_time: float
    meter: Tuple[int, int]
    tempo_bpm: float


@dataclass(frozen=True)
class _SongMap:
    bars: List[_Bar]
    global_bpm_estimate: float


@dataclass(frozen=True)
class ProductionCandidate:
    role: str
    event_stream: List[Dict[str, Any]]
    performance_spec: Optional[Dict[str, Any]]
    tempo_bpm: float
    time_signature: Dict[str, Any]
    bars: int
    kit_id: str
    base_groove_path: str
    metadata: Dict[str, Any]
    profile_snapshot: Optional[Dict[str, Any]] = None

    @property
    def note_count(self) -> int:
        return len(self.event_stream)


_ALLOWED_CFG_RANGES: Dict[str, Tuple[float, float]] = {
    "humanizeAmount": (0.0, 1.0),
    "ghostNoteAmount": (0.0, 1.0),
    "swingAmount": (0.0, 1.0),
    "intensity": (0.0, 1.0),
    "variation": (0.0, 1.0),
    "complexity": (0.0, 1.0),
    "fillDensity": (0.0, 1.0),
}
_ALLOWED_CFG_SCALARS = {
    "style",
    "fillType",
    "generationMode",
    "articulationProfile",
    "styleSourceMode",
}


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_time_signature(value: Any) -> Tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return max(1, int(value[0])), max(1, int(value[1]))
        except Exception:
            pass
    text = str(value or "4/4")
    if "/" in text:
        left, right = text.split("/", 1)
        try:
            return max(1, int(left)), max(1, int(right))
        except Exception:
            pass
    return 4, 4


def _deep_merge(base: Dict[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def validate_cfg_overrides(overrides: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not overrides:
        return {}
    result: Dict[str, Any] = {}
    for key, value in overrides.items():
        if key in _ALLOWED_CFG_RANGES:
            lo, hi = _ALLOWED_CFG_RANGES[key]
            numeric = float(value)
            if not lo <= numeric <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}")
            result[key] = numeric
        elif key in _ALLOWED_CFG_SCALARS:
            result[key] = str(value)
        else:
            raise ValueError(f"Treatment may not override production cfg key '{key}'")
    return result


def validate_treatment_overrides(
    *,
    cfg_overrides: Optional[Mapping[str, Any]],
    profile_overrides: Optional[Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Return normalized bounded treatment deltas or raise ``ValueError``."""
    return {
        "cfg_overrides": validate_cfg_overrides(cfg_overrides),
        "profile_overrides": validate_profile_overrides(profile_overrides),
    }


def _resolve_base_groove_path(base_groove_id: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    raw = Path(str(base_groove_id or "").strip())
    candidates = [
        raw,
        root / "tests" / "assets" / f"{base_groove_id}.json",
        root / "tests" / "assets" / "base_groove.json",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(f"Could not resolve base groove '{base_groove_id}'")


def _load_pattern(base_groove_id: str) -> PatternAsset:
    path = _resolve_base_groove_path(base_groove_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = payload.get("pattern_events")
    if not isinstance(events, list) or not events:
        raise ValueError(f"Base groove has no pattern_events: {path}")
    return PatternAsset(
        tempo_bpm=float(payload.get("tempo_bpm", 110.0)),
        time_signature=_parse_time_signature(payload.get("time_signature", "4/4")),
        ppqn=int(payload.get("ppqn", 960)),
        events=[dict(event) for event in events if isinstance(event, dict)],
        source_path=str(path),
    )


def _repeat_events(events: Iterable[Dict[str, Any]], repeats: int) -> List[Dict[str, Any]]:
    source = [dict(item) for item in events]
    if not source:
        return []
    repeat_count = max(1, int(repeats))
    source_bars = sorted({int(item.get("barIndex", 0)) for item in source})
    bar_count = max(1, len(source_bars))
    minimum_start = min(float(item.get("barStartTime", 0.0)) for item in source)
    maximum_end = max(float(item.get("barEndTime", item.get("time_sec", 0.0))) for item in source)
    duration = max(0.001, maximum_end - minimum_start)

    output: List[Dict[str, Any]] = []
    for index in range(repeat_count):
        for event in source:
            item = dict(event)
            item["barIndex"] = int(event.get("barIndex", 0)) + index * bar_count
            for key in ("barStartTime", "barEndTime", "time_sec"):
                if key in event:
                    item[key] = float(event[key]) + index * duration
            output.append(item)
    return output


def _bar_count(events: Iterable[Dict[str, Any]]) -> int:
    values = [int(item.get("barIndex", 0)) for item in events]
    return (max(values) + 1) if values else 0


def _build_songmap(
    *, events: List[Dict[str, Any]], tempo_bpm: float, meter: Tuple[int, int]
) -> _SongMap:
    count = _bar_count(events)
    if count <= 0:
        raise ValueError("Cannot build song map for an empty event stream")
    numerator, denominator = meter
    seconds_per_beat = 60.0 / max(1.0, float(tempo_bpm))
    bar_seconds = seconds_per_beat * numerator * (4.0 / denominator)
    bars: List[_Bar] = []
    for bar_index in range(count):
        in_bar = [item for item in events if int(item.get("barIndex", 0)) == bar_index]
        start = min((float(item.get("barStartTime", bar_index * bar_seconds)) for item in in_bar), default=bar_index * bar_seconds)
        end = max((float(item.get("barEndTime", start + bar_seconds)) for item in in_bar), default=start + bar_seconds)
        if end <= start:
            end = start + bar_seconds
        bars.append(_Bar(start_time=start, end_time=end, meter=meter, tempo_bpm=tempo_bpm))
    return _SongMap(bars=bars, global_bpm_estimate=tempo_bpm)


def _to_internal_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for event in events:
        instrument = str(event.get("instrument_id") or event.get("instrumentId") or "").strip()
        if not instrument:
            raise ValueError(f"Base event has no instrument: {event}")
        output.append(
            {
                "time_sec": float(event.get("time_sec", 0.0)),
                "length_sec": float(event.get("length_sec", 0.08)),
                "instrument_id": instrument,
                "midi_pitch": int(event.get("midi_pitch", instrument_id_to_midi_pitch(instrument))),
                "velocity": max(1, min(127, int(round(float(event.get("velocity", 96)))))),
                "isGhost": bool(event.get("isGhost", event.get("is_ghost", False))),
                "isAccent": bool(event.get("isAccent", event.get("is_accent", False))),
                "isFlam": bool(event.get("isFlam", event.get("is_flam", False))),
                "isDrag": bool(event.get("isDrag", event.get("is_drag", False))),
            }
        )
    return output


def _from_dcsm_notes(track: Any, songmap: _SongMap, ppqn: int) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for note in track.notes:
        bar_index = int(note.barIndex)
        bar = songmap.bars[bar_index]
        bar_ticks = max(1, ppqn * bar.meter[0])
        frac = max(0.0, min(0.999999, float(note.tickInBar) / float(bar_ticks)))
        time_sec = bar.start_time + frac * (bar.end_time - bar.start_time)
        output.append(
            {
                "barIndex": bar_index,
                "barStartTime": bar.start_time,
                "barEndTime": bar.end_time,
                "bar_pos_frac": frac,
                "time_sec": time_sec,
                "length_sec": max(0.01, float(note.tickLength) / ppqn * (60.0 / bar.tempo_bpm)),
                "instrument_id": str(note.instrumentId),
                "midi_pitch": int(note.midiPitch),
                "velocity": int(note.velocity),
                "isGhost": bool(note.isGhost),
                "isAccent": bool(note.isAccent),
                "isFlam": bool(note.isFlam),
                "isDrag": bool(note.isDrag),
                "timing_offset_ms": float(note.microTimingMs or 0.0),
            }
        )
    output.sort(key=lambda item: (item["barIndex"], item["time_sec"], item["midi_pitch"]))
    return output


class CalibrationProductionEngine:
    def __init__(
        self,
        db: CentralDatabaseService,
        *,
        performance_client: Optional[ProductionPerformanceClient] = None,
        profile_resolver: Optional[CalibrationProfileResolver] = None,
    ) -> None:
        self._db = db
        self._client = performance_client or ProductionPerformanceClient()
        self._profiles = profile_resolver or CalibrationProfileResolver(db)

    def generate_neutral(
        self,
        *,
        base_groove_id: str,
        repeats: int,
        seed: int,
        kit_id: str = "default_kit",
    ) -> ProductionCandidate:
        pattern = _load_pattern(base_groove_id)
        events = _repeat_events(pattern.events, repeats)
        return ProductionCandidate(
            role="neutral",
            event_stream=events,
            performance_spec=None,
            tempo_bpm=pattern.tempo_bpm,
            time_signature={
                "display": f"{pattern.time_signature[0]}/{pattern.time_signature[1]}",
                "numerator": pattern.time_signature[0],
                "denominator": pattern.time_signature[1],
            },
            bars=_bar_count(events),
            kit_id=kit_id,
            base_groove_path=pattern.source_path,
            metadata={
                "engine": "calibration_neutral_v2",
                "role": "neutral",
                "paired_seed": int(seed),
                "base_pattern_hash": _canonical_hash(pattern.events),
                "event_stream_hash": _canonical_hash(events),
                "personality_enabled": False,
            },
            profile_snapshot=None,
        )

    def generate_candidate(
        self,
        *,
        role: str,
        base_groove_id: str,
        drummer_slug: str,
        seed: int,
        repeats: int = 4,
        cfg_overrides: Optional[Dict[str, Any]] = None,
        profile_overrides: Optional[Dict[str, Any]] = None,
        treatment_id: Optional[str] = None,
        kit_id: str = "default_kit",
    ) -> ProductionCandidate:
        if role not in {"control", "challenger"}:
            raise ValueError("role must be 'control' or 'challenger'")

        pattern = _load_pattern(base_groove_id)
        events = _repeat_events(pattern.events, repeats)
        bars = _bar_count(events)
        songmap = _build_songmap(events=events, tempo_bpm=pattern.tempo_bpm, meter=pattern.time_signature)
        profile: ResolvedCalibrationProfile = self._profiles.resolve(
            drummer_slug=drummer_slug,
            profile_overrides=profile_overrides,
            strict=True,
        )

        cfg: Dict[str, Any] = {
            "sectionId": "calibration",
            "startMeasure": 0,
            "endMeasure": max(0, bars - 1),
            "tempos": [pattern.tempo_bpm for _ in range(max(1, bars))],
            "timeSignature": list(pattern.time_signature),
            "style": str(profile.profile.get("primary_style") or profile.profile.get("style") or "rock"),
            "drummer": drummer_slug,
            "publicDrummerId": drummer_slug,
            "intensity": 0.65,
            "variation": 0.50,
            "generationMode": "ai_variation",
            "humanize": True,
            "fillLocations": [],
            "fillType": "none",
            "fillDensity": 0.0,
            "humanizeAmount": 0.70,
            "ghostNoteAmount": 0.50,
            "swingAmount": 0.0,
            "buildScope": "selected_section",
            "guideEnabled": False,
            "complexity": 0.50,
            "resolutionPpq": pattern.ppqn,
            "styleSourceMode": "combined",
        }
        cfg = _deep_merge(cfg, validate_cfg_overrides(cfg_overrides))
        songmap_summary = {
            "styleGroup": cfg["style"],
            "sections": [
                {
                    "label": "calibration_groove",
                    "sectionType": "groove",
                    "startBar": 0,
                    "endBar": max(0, bars - 1),
                    "energy": float(cfg["intensity"]),
                }
            ],
        }

        spec_result = self._client.generate_performance_spec(
            cfg=cfg,
            songmap_summary=songmap_summary,
            drummer_profile=profile.profile,
        )
        rng = random.Random(int(seed))
        builder_kwargs = {
            "songmap": songmap,
            "internal_drum_events": _to_internal_events(events),
            "style_id": str(cfg["style"]),
            "performance_spec": spec_result.spec,
            "resolution_ppq": pattern.ppqn,
            "rng": rng,
        }
        try:
            track = build_drumtrack_for_dcsm(**builder_kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'rng'" not in str(exc):
                raise
            builder_kwargs.pop("rng", None)
            random_state = random.getstate()
            random.seed(int(seed))
            try:
                track = build_drumtrack_for_dcsm(**builder_kwargs)
            finally:
                random.setstate(random_state)
        output_events = _from_dcsm_notes(track, songmap, pattern.ppqn)
        if not output_events:
            raise RuntimeError("Production DCSM builder returned no events")

        metadata = {
            "engine": "production_performance_spec_v2",
            "production_endpoint": spec_result.endpoint,
            "production_engine_mode": spec_result.engine_mode,
            "production_metadata": spec_result.metadata,
            "role": role,
            "treatment_id": treatment_id,
            "paired_seed": int(seed),
            "base_pattern_hash": _canonical_hash(pattern.events),
            "profile_snapshot_hash": profile.snapshot_hash,
            "rollup_version": profile.rollup_version,
            "profile_source_counts": profile.source_counts,
            "cfg_snapshot": cfg,
            "cfg_hash": _canonical_hash(cfg),
            "performance_spec_hash": _canonical_hash(spec_result.spec),
            "event_stream_hash": _canonical_hash(output_events),
            "personality_enabled": True,
        }
        return ProductionCandidate(
            role=role,
            event_stream=output_events,
            performance_spec=spec_result.spec,
            tempo_bpm=pattern.tempo_bpm,
            time_signature={
                "display": f"{pattern.time_signature[0]}/{pattern.time_signature[1]}",
                "numerator": pattern.time_signature[0],
                "denominator": pattern.time_signature[1],
            },
            bars=bars,
            kit_id=kit_id,
            base_groove_path=pattern.source_path,
            metadata=metadata,
            profile_snapshot=deepcopy(profile.profile),
        )

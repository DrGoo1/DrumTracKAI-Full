"""Performer-level BassTracKAI technique rollups."""
from __future__ import annotations
from dataclasses import dataclass
from statistics import mean, median
from .bass_features import BassFeatureSet

@dataclass(frozen=True)
class BassPerformerRollup:
    performer_profile_id: str
    source_count: int
    mean_kick_lock: float
    mean_chord_tone_ratio: float
    median_note_duration_sec: float
    dominant_techniques: tuple[str,...]
    dominant_articulations: tuple[str,...]
    rollup_version: str='bass-rollup-v1'
    execution_authorized: bool=False

def build_performer_rollup(performer_profile_id: str, feature_sets: list[BassFeatureSet]) -> BassPerformerRollup:
    if not feature_sets: raise ValueError('at least one feature set is required')
    technique_counts: dict[str,int]={}; articulation_counts: dict[str,int]={}
    for f in feature_sets:
        for tag in f.technique_tags: technique_counts[tag]=technique_counts.get(tag,0)+1
        for tag,count in f.articulation_histogram.items(): articulation_counts[tag]=articulation_counts.get(tag,0)+int(count)
    techniques=tuple(k for k,_ in sorted(technique_counts.items(), key=lambda kv:(-kv[1],kv[0])))
    articulations=tuple(k for k,_ in sorted(articulation_counts.items(), key=lambda kv:(-kv[1],kv[0])))
    harmonic=[f.chord_tone_ratio for f in feature_sets if f.chord_tone_ratio is not None]
    chord_ratio=round(mean(harmonic),6) if harmonic else 0.0
    return BassPerformerRollup(performer_profile_id,len(feature_sets),round(mean(f.kick_lock_score for f in feature_sets),6),chord_ratio,round(median(f.median_duration_sec for f in feature_sets),6),techniques,articulations)

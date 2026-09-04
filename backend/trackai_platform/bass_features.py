"""Canonical BassTracKAI event and feature extraction.

Both MIDI and audio-transcription adapters normalize into BassNoteEvent so the
assimilation layer is independent of any specific transcription provider.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from hashlib import sha256
import json, math
from statistics import median
from typing import Iterable, Mapping, Sequence

@dataclass(frozen=True)
class BassNoteEvent:
    onset_sec: float
    duration_sec: float
    midi_note: int
    velocity: float = 0.75
    confidence: float = 1.0
    articulation: str = "normal"

    def validate(self) -> None:
        if self.onset_sec < 0 or self.duration_sec <= 0: raise ValueError("invalid note timing")
        if not 0 <= self.midi_note <= 127: raise ValueError("midi_note must be 0..127")
        if not 0 <= self.velocity <= 1: raise ValueError("velocity must be 0..1")
        if not 0 <= self.confidence <= 1: raise ValueError("confidence must be 0..1")

@dataclass(frozen=True)
class BassFeatureSet:
    event_count: int
    pitch_min: int
    pitch_max: int
    median_duration_sec: float
    short_note_ratio: float
    long_note_ratio: float
    kick_lock_score: float
    syncopation_ratio: float
    chord_tone_ratio: float | None
    chromatic_approach_ratio: float | None
    articulation_histogram: dict[str, int]
    technique_tags: tuple[str, ...]
    extractor_version: str = "bass-features-v1"

    def fingerprint(self) -> str:
        raw=json.dumps(asdict(self), sort_keys=True, separators=(",",":"))
        return sha256(raw.encode()).hexdigest()


def normalize_midi_notes(notes: Iterable[Mapping[str, object]]) -> tuple[BassNoteEvent, ...]:
    out=[]
    for row in notes:
        event=BassNoteEvent(
            onset_sec=float(row["onset_sec"]), duration_sec=float(row["duration_sec"]),
            midi_note=int(row["midi_note"]), velocity=float(row.get("velocity", 0.75)),
            confidence=1.0, articulation=str(row.get("articulation", "normal")),
        ); event.validate(); out.append(event)
    return tuple(sorted(out, key=lambda x:(x.onset_sec,x.midi_note)))


def normalize_audio_events(events: Iterable[Mapping[str, object]], *, minimum_confidence: float=.55) -> tuple[BassNoteEvent, ...]:
    out=[]
    for row in events:
        confidence=float(row.get("confidence", 0))
        if confidence < minimum_confidence: continue
        event=BassNoteEvent(
            onset_sec=float(row["onset_sec"]), duration_sec=float(row["duration_sec"]),
            midi_note=int(row["midi_note"]), velocity=float(row.get("velocity", 0.75)),
            confidence=confidence, articulation=str(row.get("articulation", "normal")),
        ); event.validate(); out.append(event)
    return tuple(sorted(out, key=lambda x:(x.onset_sec,x.midi_note)))


def _nearest_distance(value: float, anchors: Sequence[float]) -> float:
    return min((abs(value-x) for x in anchors), default=math.inf)


def extract_bass_features(
    events: Sequence[BassNoteEvent], *, tempo_bpm: float, kick_onsets_sec: Sequence[float]=(),
    chord_pitch_classes: Sequence[set[int]] | None=None, chord_index_for_event: Sequence[int] | None=None,
) -> BassFeatureSet:
    if tempo_bpm <= 0: raise ValueError("tempo_bpm must be positive")
    if not events: raise ValueError("at least one bass event is required")
    for event in events: event.validate()
    beat_sec=60.0/tempo_bpm; lock_window=min(.075, beat_sec*.16)
    kick_lock=sum(_nearest_distance(e.onset_sec,kick_onsets_sec)<=lock_window for e in events)/len(events) if kick_onsets_sec else 0.0
    grid=beat_sec/2.0
    syncopated=sum(abs((e.onset_sec/grid)-round(e.onset_sec/grid))>.22 for e in events)/len(events)
    durations=[e.duration_sec for e in events]
    short_ratio=sum(d < beat_sec*.35 for d in durations)/len(durations)
    long_ratio=sum(d > beat_sec*1.25 for d in durations)/len(durations)
    hist: dict[str,int]={}
    for e in events: hist[e.articulation]=hist.get(e.articulation,0)+1
    chord_ratio=None; approach_ratio=None
    if chord_pitch_classes is not None and chord_index_for_event is not None and len(chord_index_for_event)==len(events):
        valid=[]; approaches=0
        for i,e in enumerate(events):
            ci=chord_index_for_event[i]
            if 0 <= ci < len(chord_pitch_classes):
                pcs=chord_pitch_classes[ci]; pc=e.midi_note%12; valid.append(pc in pcs)
                if i+1 < len(events) and not (pc in pcs):
                    nxt=events[i+1].midi_note%12
                    if nxt in pcs and min((pc-nxt)%12,(nxt-pc)%12)<=2: approaches+=1
        if valid: chord_ratio=sum(valid)/len(valid); approach_ratio=approaches/len(valid)
    tags=[]
    if kick_lock>=.65: tags.append("kick_locked")
    if syncopated>=.30: tags.append("syncopated")
    if short_ratio>=.35: tags.append("muted_or_staccato")
    if long_ratio>=.35: tags.append("sustained")
    if chord_ratio is not None and chord_ratio>=.82: tags.append("chord_tone_grounded")
    if approach_ratio is not None and approach_ratio>=.12: tags.append("chromatic_approach")
    if any(k in hist for k in ("slide","hammer_on","pull_off")): tags.append("legato_articulation")
    return BassFeatureSet(
        event_count=len(events), pitch_min=min(e.midi_note for e in events), pitch_max=max(e.midi_note for e in events),
        median_duration_sec=round(float(median(durations)),6), short_note_ratio=round(short_ratio,6), long_note_ratio=round(long_ratio,6),
        kick_lock_score=round(kick_lock,6), syncopation_ratio=round(syncopated,6),
        chord_tone_ratio=None if chord_ratio is None else round(chord_ratio,6),
        chromatic_approach_ratio=None if approach_ratio is None else round(approach_ratio,6),
        articulation_histogram=hist, technique_tags=tuple(tags),
    )


def observation_from_features(*, source_id: str, performer_profile_id: str, provenance_uri: str,
                              tempo_bpm: float, meter: str, features: BassFeatureSet,
                              key_center: str | None=None, chord_map_id: str | None=None):
    """Build the existing ingestion observation from canonical extracted features."""
    from .bass_contracts import BassSourceObservation
    duration_profile=(
        "staccato" if features.short_note_ratio >= .35 else
        "sustained" if features.long_note_ratio >= .35 else "mixed"
    )
    articulations=tuple(sorted(k for k,v in features.articulation_histogram.items() if v>0))
    return BassSourceObservation(
        source_id=source_id, performer_profile_id=performer_profile_id,
        provenance_uri=provenance_uri, tempo_bpm=tempo_bpm, meter=meter,
        key_center=key_center, chord_map_id=chord_map_id,
        kick_alignment_score=features.kick_lock_score,
        note_length_profile=duration_profile,
        articulation_tags=articulations, technique_tags=features.technique_tags,
        extraction_version=features.extractor_version,
    )

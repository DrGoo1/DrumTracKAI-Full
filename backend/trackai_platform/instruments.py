"""Shared instrument registry for the TracKAI research/calibration platform.

The registry deliberately keeps current DrumTracKAI storage/wire contracts intact
while exposing generic instrument semantics for new products such as BassTracKAI.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

InstrumentId = Literal["drums", "bass", "guitar", "keys", "horns", "vocal"]

@dataclass(frozen=True)
class RatingDimension:
    key: str
    label: str
    description: str

@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: InstrumentId
    product_id: str
    display_name: str
    subject_label: str
    source_entity_label: str
    generation_role: str
    ratings: tuple[RatingDimension, ...]
    conditioning_inputs: tuple[str, ...]
    execution_authorized: bool = False

COMMON = (
    RatingDimension("stylistic_authenticity", "Stylistic authenticity", "Does the performance match the intended style and technique profile?"),
    RatingDimension("groove_feel", "Groove and pocket", "Does the timing feel musical and intentional?"),
    RatingDimension("dynamics", "Dynamic touch", "Are dynamics expressive and contextually appropriate?"),
    RatingDimension("phrasing", "Phrasing", "Does phrase shape support the musical section?"),
    RatingDimension("human_realism", "Human realism", "Does the result avoid mechanical or implausible behavior?"),
    RatingDimension("overall_usefulness", "Overall usefulness", "Would this performance be useful in a production?"),
)

DRUMS = InstrumentSpec(
    instrument_id="drums", product_id="drumtrackai", display_name="DrumTracKAI",
    subject_label="Drummer / technique profile", source_entity_label="drummer",
    generation_role="rhythm foundation",
    ratings=COMMON[:4] + (
        RatingDimension("kit_balance", "Kit balance", "Is energy distributed naturally across the kit?"),
        RatingDimension("fill_behavior", "Fill behavior", "Are fills idiomatic, well placed, and structurally appropriate?"),
    ) + COMMON[4:],
    conditioning_inputs=("tempo_meter", "section_map", "guide_audio", "bass_context"),
)

BASS = InstrumentSpec(
    instrument_id="bass", product_id="basstrackai", display_name="BassTracKAI",
    subject_label="Bassist / technique profile", source_entity_label="bassist",
    generation_role="harmonic-rhythmic foundation",
    ratings=COMMON[:4] + (
        RatingDimension("kick_lock", "Kick-lock relationship", "Does the bass interact intentionally with kick events and pocket?"),
        RatingDimension("note_length", "Note-length behavior", "Are sustain, muting, rests, and releases musically convincing?"),
        RatingDimension("harmonic_accuracy", "Harmonic accuracy", "Do notes respect harmony while using idiomatic approaches and extensions?"),
        RatingDimension("articulation", "Bass articulation", "Are ghost notes, mutes, slides, attacks, and transitions idiomatic?"),
    ) + COMMON[4:],
    conditioning_inputs=("tempo_meter", "section_map", "chord_map", "kick_events", "drum_groove", "vocal_melody", "existing_instruments"),
)

_REGISTRY = {spec.instrument_id: spec for spec in (DRUMS, BASS)}

def get_instrument_spec(instrument_id: str) -> InstrumentSpec:
    try:
        return _REGISTRY[instrument_id]
    except KeyError as exc:
        raise ValueError(f"unsupported TracKAI instrument: {instrument_id}") from exc

def available_instrument_specs() -> tuple[InstrumentSpec, ...]:
    return tuple(_REGISTRY.values())

"""BassTracKAI source-assimilation and generation-plan contracts.

Contracts are non-executable by default and designed for StudioMind handoff.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Literal

@dataclass(frozen=True)
class BassSourceObservation:
    source_id: str
    performer_profile_id: str
    provenance_uri: str
    tempo_bpm: float
    meter: str
    key_center: str | None
    chord_map_id: str | None
    kick_alignment_score: float
    note_length_profile: str
    articulation_tags: tuple[str, ...]
    technique_tags: tuple[str, ...]
    extraction_version: str

    def fingerprint(self) -> str:
        raw=json.dumps(asdict(self), sort_keys=True, separators=(",",":"))
        return sha256(raw.encode()).hexdigest()

@dataclass(frozen=True)
class BassAssimilationProfile:
    profile_id: str
    performer_profile_id: str
    source_fingerprints: tuple[str, ...]
    technique_clusters: tuple[str, ...]
    groove_traits: tuple[str, ...]
    harmonic_traits: tuple[str, ...]
    articulation_traits: tuple[str, ...]
    readiness: Literal["blocked","review","calibration_ready"] = "blocked"
    human_review_required: bool = True
    execution_authorized: bool = False

@dataclass(frozen=True)
class BassGenerationPlan:
    plan_id: str
    profile_id: str
    tempo_bpm: float
    meter: str
    chord_map_id: str
    section_map_id: str
    kick_events_id: str
    role: Literal["supportive","melodic","aggressive","sparse","syncopated"]
    density: float
    humanization: float
    requested_articulations: tuple[str, ...]
    provider_version: str | None = None
    model_version: str | None = None
    approved_for_generation: bool = False
    execution_authorized: bool = False

    def validate(self) -> None:
        if not 0 <= self.density <= 1: raise ValueError("density must be 0..1")
        if not 0 <= self.humanization <= 1: raise ValueError("humanization must be 0..1")
        if self.approved_for_generation and (not self.provider_version or not self.model_version):
            raise ValueError("approved plan requires exact provider and model versions")
        if self.execution_authorized:
            raise ValueError("BassTracKAI execution is not certified")

def build_bass_plan(**kwargs) -> BassGenerationPlan:
    plan=BassGenerationPlan(**kwargs)
    plan.validate()
    return plan

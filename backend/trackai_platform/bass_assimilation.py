"""Fail-closed BassTracKAI ingestion and assimilation service."""
from __future__ import annotations
from dataclasses import dataclass
from .source_intake import SourceEvidence, SourceEvidenceRepository
from .bass_contracts import BassAssimilationProfile, BassSourceObservation

REQUIRED_FEATURES=("tempo_bpm","meter","kick_alignment_score","note_length_profile","articulation_tags","technique_tags")

@dataclass(frozen=True)
class BassDatasetStatus:
    performer_profile_id: str
    source_count: int
    reviewed_source_count: int
    extraction_versions: tuple[str,...]
    readiness: str
    blockers: tuple[str,...]
    execution_authorized: bool=False

class BassAssimilationService:
    def __init__(self, repository: SourceEvidenceRepository, *, minimum_sources: int=6) -> None:
        self.repository=repository; self.minimum_sources=minimum_sources
    def ingest_observation(self, observation: BassSourceObservation, *, source_title: str, human_reviewed: bool=False) -> str:
        payload={
            "tempo_bpm":observation.tempo_bpm,"meter":observation.meter,"key_center":observation.key_center,
            "chord_map_id":observation.chord_map_id,"kick_alignment_score":observation.kick_alignment_score,
            "note_length_profile":observation.note_length_profile,"articulation_tags":list(observation.articulation_tags),
            "technique_tags":list(observation.technique_tags),
        }
        if not observation.provenance_uri.strip(): raise ValueError("provenance_uri is required")
        if not 0 <= observation.kick_alignment_score <= 1: raise ValueError("kick_alignment_score must be 0..1")
        return self.repository.put(SourceEvidence("bass",observation.source_id,observation.performer_profile_id,observation.provenance_uri,source_title,observation.extraction_version,payload,human_reviewed))
    def status(self, performer_profile_id: str) -> BassDatasetStatus:
        items=self.repository.list_for_subject("bass",performer_profile_id)
        reviewed=sum(1 for x in items if x.human_reviewed)
        blockers=[]
        if len(items)<self.minimum_sources: blockers.append(f"minimum_sources:{self.minimum_sources}")
        if reviewed<len(items): blockers.append("human_review_incomplete")
        versions=tuple(sorted({x.extraction_version for x in items}))
        if len(versions)>1: blockers.append("mixed_extraction_versions")
        readiness="calibration_ready" if items and not blockers else ("review" if items else "blocked")
        return BassDatasetStatus(performer_profile_id,len(items),reviewed,versions,readiness,tuple(blockers))
    def build_profile(self, performer_profile_id: str) -> BassAssimilationProfile:
        status=self.status(performer_profile_id)
        items=self.repository.list_for_subject("bass",performer_profile_id)
        if status.readiness!="calibration_ready": raise ValueError("Bass dataset is not calibration ready")
        techniques=sorted({str(v) for x in items for v in x.feature_payload.get("technique_tags",[])})
        articulations=sorted({str(v) for x in items for v in x.feature_payload.get("articulation_tags",[])})
        return BassAssimilationProfile(
            profile_id=f"bass-profile:{performer_profile_id}", performer_profile_id=performer_profile_id,
            source_fingerprints=tuple(x.fingerprint() for x in items), technique_clusters=tuple(techniques),
            groove_traits=("kick_relationship",), harmonic_traits=("source_harmony_conditioned",),
            articulation_traits=tuple(articulations), readiness="calibration_ready")

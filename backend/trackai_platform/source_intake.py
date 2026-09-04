"""Instrument-neutral source intake and evidence storage contracts."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
from typing import Protocol

@dataclass(frozen=True)
class SourceEvidence:
    instrument_id: str
    source_id: str
    subject_profile_id: str
    provenance_uri: str
    source_title: str
    extraction_version: str
    feature_payload: dict[str, object]
    human_reviewed: bool = False

    def fingerprint(self) -> str:
        raw=json.dumps(asdict(self), sort_keys=True, separators=(",",":"))
        return sha256(raw.encode()).hexdigest()

class SourceEvidenceRepository(Protocol):
    def put(self, evidence: SourceEvidence) -> str: ...
    def list_for_subject(self, instrument_id: str, subject_profile_id: str) -> tuple[SourceEvidence, ...]: ...

class InMemorySourceEvidenceRepository:
    def __init__(self) -> None: self._items: dict[str, SourceEvidence] = {}
    def put(self, evidence: SourceEvidence) -> str:
        fp=evidence.fingerprint(); self._items[fp]=evidence; return fp
    def list_for_subject(self, instrument_id: str, subject_profile_id: str) -> tuple[SourceEvidence, ...]:
        return tuple(x for x in self._items.values() if x.instrument_id==instrument_id and x.subject_profile_id==subject_profile_id)

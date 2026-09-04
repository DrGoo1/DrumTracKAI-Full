"""Instrument-neutral source intake and evidence storage contracts."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from hashlib import sha256
import json
from typing import Protocol
from pathlib import Path

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

class JsonSourceEvidenceRepository:
    """Atomic JSON repository for restart-safe TracKAI source evidence."""
    def __init__(self, root: str | Path) -> None:
        from pathlib import Path
        self.root=Path(root)
    def _path(self, instrument_id: str, subject_profile_id: str, source_id: str):
        return self.root/instrument_id/subject_profile_id/f'{source_id}.json'
    def put(self, evidence: SourceEvidence) -> str:
        path=self._path(evidence.instrument_id,evidence.subject_profile_id,evidence.source_id)
        path.parent.mkdir(parents=True,exist_ok=True)
        payload=asdict(evidence); digest=evidence.fingerprint(); body={'payload':payload,'sha256':digest}
        tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(body,sort_keys=True,indent=2)); tmp.replace(path)
        return digest
    def _load(self, path):
        body=json.loads(path.read_text()); payload=body['payload']
        raw=json.dumps(payload,sort_keys=True,separators=(',',':'))
        if sha256(raw.encode()).hexdigest()!=body.get('sha256'):
            raise ValueError('TracKAI source evidence integrity check failed')
        return SourceEvidence(**payload)
    def list_for_subject(self, instrument_id: str, subject_profile_id: str) -> tuple[SourceEvidence,...]:
        directory=self.root/instrument_id/subject_profile_id
        if not directory.exists(): return ()
        return tuple(self._load(p) for p in sorted(directory.glob('*.json')))
    def mark_human_reviewed(self, instrument_id: str, subject_profile_id: str, source_id: str, reviewed: bool) -> None:
        path=self._path(instrument_id,subject_profile_id,source_id)
        current=self._load(path)
        self.put(SourceEvidence(current.instrument_id,current.source_id,current.subject_profile_id,current.provenance_uri,current.source_title,current.extraction_version,current.feature_payload,bool(reviewed)))

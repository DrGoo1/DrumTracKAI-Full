"""Persistent BassTracKAI feature artifacts with tamper detection."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from .bass_features import BassFeatureSet

@dataclass(frozen=True)
class BassFeatureArtifact:
    source_id: str
    performer_profile_id: str
    provenance_uri: str
    extractor_version: str
    features: BassFeatureSet
    artifact_version: str = "bass-feature-artifact-v1"

    def payload(self) -> dict:
        return {
            "artifact_version": self.artifact_version,
            "source_id": self.source_id,
            "performer_profile_id": self.performer_profile_id,
            "provenance_uri": self.provenance_uri,
            "extractor_version": self.extractor_version,
            "features": asdict(self.features),
        }

    def digest(self) -> str:
        raw=json.dumps(self.payload(),sort_keys=True,separators=(",",":"))
        return sha256(raw.encode()).hexdigest()

class JsonBassFeatureArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root=Path(root)
    def put(self, artifact: BassFeatureArtifact) -> Path:
        self.root.mkdir(parents=True,exist_ok=True)
        body={"payload":artifact.payload(),"sha256":artifact.digest()}
        path=self.root/f"{artifact.source_id}.json"
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(body,sort_keys=True,indent=2))
        tmp.replace(path)
        return path
    def load_payload(self, source_id: str) -> dict:
        path=self.root/f"{source_id}.json"
        body=json.loads(path.read_text())
        payload=body["payload"]
        raw=json.dumps(payload,sort_keys=True,separators=(",",":"))
        if sha256(raw.encode()).hexdigest()!=body.get("sha256"):
            raise ValueError("Bass feature artifact integrity check failed")
        return payload
    def list_payloads(self) -> list[dict]:
        if not self.root.exists(): return []
        return [self.load_payload(path.stem) for path in sorted(self.root.glob("*.json"))]

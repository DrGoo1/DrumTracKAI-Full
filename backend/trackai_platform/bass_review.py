"""Persistent human review state for BassTracKAI source evidence."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

ReviewDecision = Literal['accepted','rejected','needs_revision']

@dataclass(frozen=True)
class BassSourceReview:
    source_id: str
    performer_profile_id: str
    reviewer_id: str
    decision: ReviewDecision
    confidence: float
    notes: str = ''
    review_version: str = 'bass-source-review-v1'

    def validate(self) -> None:
        if not self.reviewer_id.strip(): raise ValueError('reviewer_id is required')
        if not 0 <= self.confidence <= 1: raise ValueError('confidence must be 0..1')

    def digest(self) -> str:
        self.validate()
        raw=json.dumps(asdict(self),sort_keys=True,separators=(',',':'))
        return sha256(raw.encode()).hexdigest()

class JsonBassSourceReviewStore:
    def __init__(self, root: str | Path) -> None: self.root=Path(root)
    def put(self, review: BassSourceReview) -> Path:
        review.validate(); self.root.mkdir(parents=True,exist_ok=True)
        payload=asdict(review); body={'payload':payload,'sha256':review.digest()}
        path=self.root/f'{review.performer_profile_id}__{review.source_id}.json'
        tmp=path.with_suffix('.tmp'); tmp.write_text(json.dumps(body,sort_keys=True,indent=2)); tmp.replace(path)
        return path
    def list_for_performer(self, performer_profile_id: str) -> tuple[BassSourceReview,...]:
        if not self.root.exists(): return ()
        out=[]
        for path in sorted(self.root.glob(f'{performer_profile_id}__*.json')):
            body=json.loads(path.read_text()); payload=body['payload']
            raw=json.dumps(payload,sort_keys=True,separators=(',',':'))
            if sha256(raw.encode()).hexdigest()!=body.get('sha256'): raise ValueError('Bass review integrity check failed')
            out.append(BassSourceReview(**payload))
        return tuple(out)

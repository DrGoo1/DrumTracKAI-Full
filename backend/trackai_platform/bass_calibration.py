"""Blinded BassTracKAI A/B calibration contracts."""
from __future__ import annotations
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal

CandidateLabel=Literal['A','B']

@dataclass(frozen=True)
class BassCalibrationCandidate:
    candidate_id: str
    plan_id: str
    provider_version: str
    model_version: str
    artifact_uri: str
    artifact_sha256: str
    execution_authorized: bool=False

@dataclass(frozen=True)
class BassCalibrationTrial:
    trial_id: str
    performer_profile_id: str
    neutral_artifact_uri: str
    candidate_a: BassCalibrationCandidate
    candidate_b: BassCalibrationCandidate
    rubric_version: str='bass-calibration-rubric-v1'
    blinded: bool=True
    execution_authorized: bool=False

    def fingerprint(self) -> str:
        payload={'trial_id':self.trial_id,'performer_profile_id':self.performer_profile_id,'neutral_artifact_uri':self.neutral_artifact_uri,'a':self.candidate_a.__dict__,'b':self.candidate_b.__dict__,'rubric_version':self.rubric_version,'blinded':self.blinded}
        return sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()

@dataclass(frozen=True)
class BassCalibrationJudgment:
    trial_id: str
    reviewer_id: str
    preferred_candidate: CandidateLabel
    closer_to_target: CandidateLabel
    better_pocket: CandidateLabel
    better_harmony: CandidateLabel
    better_articulation: CandidateLabel
    confidence: float
    listening_ms: int

    def validate(self) -> None:
        if not 0 <= self.confidence <= 1: raise ValueError('confidence must be 0..1')
        if self.listening_ms <= 0: raise ValueError('listening_ms must be positive')

"""Research-only BassTracKAI candidate render -> blind-review loop.

This module deliberately stops at artifact registration. It never invokes a model,
renderer, DAW, or promotion path and therefore grants no generation authority.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Literal
from .bass_calibration import BassCalibrationCandidate, BassCalibrationTrial
from .bass_contracts import BassGenerationPlan

DIGEST_LEN=64

@dataclass(frozen=True)
class BassCandidateRenderRequest:
    request_id: str
    plan_id: str
    performer_profile_id: str
    provider_version: str
    model_version: str
    neutral_context_sha256: str
    seed: int
    candidate_slot: Literal['A','B']
    renderer_id: str
    execution_authorized: bool=False

    def validate(self) -> None:
        if len(self.neutral_context_sha256)!=DIGEST_LEN: raise ValueError('neutral context must be SHA-256')
        if self.seed < 0: raise ValueError('seed must be nonnegative')
        if self.execution_authorized: raise ValueError('Bass candidate render execution is not certified')

    def fingerprint(self) -> str:
        self.validate()
        return sha256(json.dumps(asdict(self),sort_keys=True,separators=(',',':')).encode()).hexdigest()

@dataclass(frozen=True)
class BassCandidateRenderReceipt:
    request_fingerprint: str
    candidate_id: str
    plan_id: str
    performer_profile_id: str
    provider_version: str
    model_version: str
    artifact_uri: str
    artifact_sha256: str
    artifact_kind: Literal['midi','audio_preview']
    candidate_slot: Literal['A','B']
    research_only: bool=True
    human_review_required: bool=True
    candidate_commit_authorized: bool=False
    model_promotion_authorized: bool=False
    daw_execution_authorized: bool=False

    def validate(self) -> None:
        if len(self.request_fingerprint)!=DIGEST_LEN or len(self.artifact_sha256)!=DIGEST_LEN: raise ValueError('receipt digests must be SHA-256')
        if not self.research_only or not self.human_review_required: raise ValueError('Bass candidate receipt must remain research-only and human-reviewed')
        if self.candidate_commit_authorized or self.model_promotion_authorized or self.daw_execution_authorized: raise ValueError('Bass candidate receipt cannot grant authority')


def prepare_candidate_render_request(plan:BassGenerationPlan, *, performer_profile_id:str, neutral_context_sha256:str, seed:int, candidate_slot:Literal['A','B'], renderer_id:str, request_id:str) -> BassCandidateRenderRequest:
    plan.validate()
    if not plan.approved_for_generation or not plan.provider_version or not plan.model_version:
        raise ValueError('candidate preparation requires an exact provider/model-bound approved research plan')
    req=BassCandidateRenderRequest(request_id,plan.plan_id,performer_profile_id,plan.provider_version,plan.model_version,neutral_context_sha256,seed,candidate_slot,renderer_id)
    req.validate(); return req


def register_rendered_candidate(request:BassCandidateRenderRequest, *, candidate_id:str, artifact_uri:str, artifact_sha256:str, artifact_kind:Literal['midi','audio_preview']) -> BassCandidateRenderReceipt:
    request.validate()
    receipt=BassCandidateRenderReceipt(request.fingerprint(),candidate_id,request.plan_id,request.performer_profile_id,request.provider_version,request.model_version,artifact_uri,artifact_sha256,artifact_kind,request.candidate_slot)
    receipt.validate(); return receipt


def build_blinded_trial(*, trial_id:str, neutral_artifact_uri:str, candidate_a:BassCandidateRenderReceipt, candidate_b:BassCandidateRenderReceipt) -> BassCalibrationTrial:
    candidate_a.validate(); candidate_b.validate()
    if candidate_a.candidate_slot!='A' or candidate_b.candidate_slot!='B': raise ValueError('candidate slots must be A and B')
    comparable=('plan_id','performer_profile_id','provider_version','model_version','artifact_kind')
    for field in comparable:
        if getattr(candidate_a,field)!=getattr(candidate_b,field): raise ValueError(f'candidate mismatch: {field}')
    a=BassCalibrationCandidate(candidate_a.candidate_id,candidate_a.plan_id,candidate_a.provider_version,candidate_a.model_version,candidate_a.artifact_uri,candidate_a.artifact_sha256)
    b=BassCalibrationCandidate(candidate_b.candidate_id,candidate_b.plan_id,candidate_b.provider_version,candidate_b.model_version,candidate_b.artifact_uri,candidate_b.artifact_sha256)
    return BassCalibrationTrial(trial_id,candidate_a.performer_profile_id,neutral_artifact_uri,a,b)

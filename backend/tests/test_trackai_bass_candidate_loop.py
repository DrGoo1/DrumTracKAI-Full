import pytest
from backend.trackai_platform.bass_contracts import BassGenerationPlan
from backend.trackai_platform.bass_candidate_loop import prepare_candidate_render_request, register_rendered_candidate, build_blinded_trial

def plan():
    return BassGenerationPlan('plan1','profile1',100,'4/4','chords','sections','kick','supportive',.5,.4,('finger',),'provider-v1','model-v1',True,False)

def receipt(slot,seed,sha):
    req=prepare_candidate_render_request(plan(),performer_profile_id='perf1',neutral_context_sha256='a'*64,seed=seed,candidate_slot=slot,renderer_id='research-renderer-v1',request_id='req'+slot)
    return register_rendered_candidate(req,candidate_id='cand'+slot,artifact_uri=f's3://research/{slot}',artifact_sha256=sha,artifact_kind='audio_preview')

def test_candidate_loop_prepares_exact_bound_but_nonexecuting_requests():
    req=prepare_candidate_render_request(plan(),performer_profile_id='perf1',neutral_context_sha256='a'*64,seed=7,candidate_slot='A',renderer_id='research-renderer-v1',request_id='r1')
    assert req.provider_version=='provider-v1' and req.model_version=='model-v1'
    assert req.execution_authorized is False

def test_registered_candidate_cannot_grant_commit_promotion_or_daw_authority():
    r=receipt('A',7,'b'*64)
    assert r.research_only and r.human_review_required
    assert not r.candidate_commit_authorized and not r.model_promotion_authorized and not r.daw_execution_authorized

def test_two_comparable_receipts_build_a_blinded_trial():
    t=build_blinded_trial(trial_id='t1',neutral_artifact_uri='s3://neutral',candidate_a=receipt('A',7,'b'*64),candidate_b=receipt('B',8,'c'*64))
    assert t.blinded and t.performer_profile_id=='perf1' and not t.execution_authorized

def test_mismatched_artifact_kind_fails_closed():
    a=receipt('A',7,'b'*64)
    req=prepare_candidate_render_request(plan(),performer_profile_id='perf1',neutral_context_sha256='a'*64,seed=8,candidate_slot='B',renderer_id='research-renderer-v1',request_id='rB')
    b=register_rendered_candidate(req,candidate_id='candB',artifact_uri='s3://b',artifact_sha256='c'*64,artifact_kind='midi')
    with pytest.raises(ValueError,match='artifact_kind'): build_blinded_trial(trial_id='t1',neutral_artifact_uri='s3://neutral',candidate_a=a,candidate_b=b)

def test_unapproved_plan_cannot_prepare_candidate():
    p=plan(); p=BassGenerationPlan(**{**p.__dict__,'approved_for_generation':False})
    with pytest.raises(ValueError,match='approved research plan'): prepare_candidate_render_request(p,performer_profile_id='perf1',neutral_context_sha256='a'*64,seed=1,candidate_slot='A',renderer_id='r',request_id='x')

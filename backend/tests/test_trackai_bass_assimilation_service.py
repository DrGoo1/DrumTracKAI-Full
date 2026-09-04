import pytest
from backend.trackai_platform.bass_assimilation import BassAssimilationService
from backend.trackai_platform.bass_contracts import BassSourceObservation
from backend.trackai_platform.source_intake import InMemorySourceEvidenceRepository

def obs(i:int, version='bass-extract-v1'):
    return BassSourceObservation(f's{i}','player1',f's3://evidence/{i}',100,'4/4','A','chords',.82,'short',('mute','ghost'),('syncopation',),version)

def test_bass_assimilation_fails_closed_until_minimum_and_review():
    svc=BassAssimilationService(InMemorySourceEvidenceRepository(),minimum_sources=2)
    svc.ingest_observation(obs(1),source_title='one',human_reviewed=True)
    assert svc.status('player1').readiness=='review'
    svc.ingest_observation(obs(2),source_title='two',human_reviewed=False)
    assert 'human_review_incomplete' in svc.status('player1').blockers
    with pytest.raises(ValueError): svc.build_profile('player1')

def test_bass_assimilation_builds_profile_only_after_reviewed_consistent_sources():
    svc=BassAssimilationService(InMemorySourceEvidenceRepository(),minimum_sources=2)
    for i in (1,2): svc.ingest_observation(obs(i),source_title=str(i),human_reviewed=True)
    status=svc.status('player1'); assert status.readiness=='calibration_ready'; assert status.execution_authorized is False
    profile=svc.build_profile('player1'); assert profile.execution_authorized is False; assert len(profile.source_fingerprints)==2

def test_mixed_extractor_versions_block_promotion():
    svc=BassAssimilationService(InMemorySourceEvidenceRepository(),minimum_sources=2)
    svc.ingest_observation(obs(1,'v1'),source_title='1',human_reviewed=True)
    svc.ingest_observation(obs(2,'v2'),source_title='2',human_reviewed=True)
    assert 'mixed_extraction_versions' in svc.status('player1').blockers

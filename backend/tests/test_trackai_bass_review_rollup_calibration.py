from pathlib import Path
from backend.trackai_platform.bass_review import BassSourceReview, JsonBassSourceReviewStore
from backend.trackai_platform.bass_features import extract_bass_features, normalize_midi_notes
from backend.trackai_platform.bass_rollup import build_performer_rollup
from backend.trackai_platform.bass_calibration import BassCalibrationCandidate, BassCalibrationTrial, BassCalibrationJudgment
from backend.trackai_platform.bass_contracts import build_bass_plan
from backend.trackai_platform.studiomind_bass_handoff import build_studiomind_bass_handoff

def feature(kick=1.0, note=36):
    notes=normalize_midi_notes([{'onset_sec':0,'duration_sec':.1,'midi_note':note,'velocity':.8,'articulation':'mute'}])
    return extract_bass_features(notes,tempo_bpm=120,kick_onsets_sec=[0] if kick else [])

def test_review_store_roundtrip(tmp_path: Path):
    store=JsonBassSourceReviewStore(tmp_path)
    review=BassSourceReview('s1','p1','reviewer','accepted',.9,'good source')
    store.put(review)
    items=store.list_for_performer('p1')
    assert len(items)==1 and items[0].decision=='accepted'

def test_rollup_is_non_executable_and_aggregates():
    r=build_performer_rollup('p1',[feature(),feature(note=40)])
    assert r.source_count==2 and r.execution_authorized is False
    assert 'kick_locked' in r.dominant_techniques

def test_calibration_trial_is_blinded_and_non_executable():
    a=BassCalibrationCandidate('a','plan','provider-1','model-1','s3://a','abc')
    b=BassCalibrationCandidate('b','plan','provider-1','model-1','s3://b','def')
    t=BassCalibrationTrial('t1','p1','s3://neutral',a,b)
    assert t.blinded and not t.execution_authorized and len(t.fingerprint())==64
    j=BassCalibrationJudgment('t1','r1','A','A','B','A','B',.8,12000); j.validate()

def test_studiomind_handoff_never_grants_execution():
    rollup=build_performer_rollup('p1',[feature()])
    plan=build_bass_plan(plan_id='plan',profile_id='p1',tempo_bpm=120,meter='4/4',chord_map_id='c',section_map_id='s',kick_events_id='k',role='supportive',density=.5,humanization=.4,requested_articulations=('mute',),provider_version='provider-1',model_version='model-1',approved_for_generation=True)
    h=build_studiomind_bass_handoff(plan,rollup,calibration_state='calibrated')
    assert h.advisory_ready is True and h.execution_authorized is False

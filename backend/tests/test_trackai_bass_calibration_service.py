from backend.trackai_platform.bass_calibration import BassCalibrationCandidate,BassCalibrationTrial,BassCalibrationJudgment
from backend.trackai_platform.bass_calibration_service import JsonBassCalibrationStore

def cand(label): return BassCalibrationCandidate('c'+label,'plan','provider1','model1',f's3://{label}','a'*64)
def test_calibration_store_and_confidence_summary(tmp_path):
    s=JsonBassCalibrationStore(tmp_path)
    for i in range(3): s.put_trial(BassCalibrationTrial(f't{i}','p1','s3://neutral',cand('A'),cand('B')))
    for i in range(3):
        for r in range(2): s.put_judgment(BassCalibrationJudgment(f't{i}',f'r{r}','A','A','A','A','A',.8,1000))
    summary=s.summary('p1')
    assert summary.calibration_state=='calibrated'; assert summary.preferred_consistency==1.0
    assert summary.rubric_agreement==1.0; assert summary.calibration_confidence==.8
    assert summary.execution_authorized is False

def test_calibration_fails_closed_without_enough_review(tmp_path):
    s=JsonBassCalibrationStore(tmp_path); s.put_trial(BassCalibrationTrial('t1','p1','s3://neutral',cand('A'),cand('B')))
    assert s.summary('p1').calibration_state!='calibrated'


def test_trial_integrity_failure_is_fail_closed(tmp_path):
    s=JsonBassCalibrationStore(tmp_path)
    s.put_trial(BassCalibrationTrial('t1','p1','s3://neutral',cand('A'),cand('B')))
    p=next((tmp_path/'trials').glob('*.json'))
    body=__import__('json').loads(p.read_text()); body['performer_profile_id']='tampered'; p.write_text(__import__('json').dumps(body))
    try:
        s.summary('p1')
    except ValueError as exc:
        assert 'integrity' in str(exc)
    else:
        raise AssertionError('expected integrity failure')

def test_rubric_disagreement_blocks_calibration(tmp_path):
    s=JsonBassCalibrationStore(tmp_path)
    for i in range(3): s.put_trial(BassCalibrationTrial(f't{i}','p1','s3://neutral',cand('A'),cand('B')))
    for i in range(3):
        for r in range(2): s.put_judgment(BassCalibrationJudgment(f't{i}',f'r{r}','A','B','B','B','B',.9,1000))
    summary=s.summary('p1')
    assert summary.calibration_state!='calibrated'
    assert 'rubric_agreement_below_floor' in summary.blockers

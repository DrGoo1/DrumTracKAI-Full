from backend import calibration_v2_api
from backend.trackai_platform.bass_calibration import BassCalibrationCandidate,BassCalibrationJudgment,BassCalibrationTrial
from backend.trackai_platform.bass_calibration_service import JsonBassCalibrationStore

def cand(label): return BassCalibrationCandidate('c'+label,'plan','provider1','model1',f's3://{label}','a'*64)

def test_bass_admin_calibration_projection_is_read_only(tmp_path):
    store=JsonBassCalibrationStore(tmp_path)
    for i in range(3):
        store.put_trial(BassCalibrationTrial(f't{i}','performer1','s3://neutral',cand('A'),cand('B')))
        for r in range(2): store.put_judgment(BassCalibrationJudgment(f't{i}',f'r{r}','A','A','A','A','A',.85,12000))
    payload=calibration_v2_api._bass_calibration_status_payload(str(tmp_path))
    assert payload['trial_count']==3 and payload['execution_authorized'] is False and payload['model_promotion_authorized'] is False
    item=payload['items'][0]
    assert item['performer_profile_id']=='performer1' and item['calibration_state']=='calibrated'
    assert item['execution_authorized'] is False and item['calibration_confidence']>0

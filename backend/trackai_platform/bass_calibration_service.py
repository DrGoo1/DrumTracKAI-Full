from __future__ import annotations
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from .bass_calibration import BassCalibrationJudgment, BassCalibrationTrial

@dataclass(frozen=True)
class BassCalibrationSummary:
    performer_profile_id: str
    trial_count: int
    judgment_count: int
    mean_confidence: float
    preferred_consistency: float
    rubric_agreement: float
    calibration_confidence: float
    calibration_state: str
    blockers: tuple[str,...]
    execution_authorized: bool=False

class JsonBassCalibrationStore:
    def __init__(self, root:str|Path): self.root=Path(root)
    def _dir(self,name:str)->Path:
        p=self.root/name; p.mkdir(parents=True,exist_ok=True); return p
    def put_trial(self, trial:BassCalibrationTrial)->str:
        payload={**asdict(trial), 'fingerprint':trial.fingerprint()}
        p=self._dir('trials')/f'{trial.trial_id}.json'; p.write_text(json.dumps(payload,sort_keys=True,indent=2)); return trial.fingerprint()
    def put_judgment(self, judgment:BassCalibrationJudgment)->str:
        judgment.validate(); payload=asdict(judgment)
        raw=json.dumps(payload,sort_keys=True,separators=(',',':')); digest=sha256(raw.encode()).hexdigest()
        key=f'{judgment.trial_id}__{judgment.reviewer_id}'
        p=self._dir('judgments')/f'{key}.json'; p.write_text(json.dumps({'payload':payload,'sha256':digest},sort_keys=True,indent=2)); return digest
    def trials(self)->list[dict]:
        out=[]
        if not (self.root/'trials').exists(): return out
        for p in sorted((self.root/'trials').glob('*.json')):
            body=json.loads(p.read_text())
            fingerprint=body.pop('fingerprint',None)
            a=body.get('candidate_a',{}); b=body.get('candidate_b',{})
            canonical={'trial_id':body['trial_id'],'performer_profile_id':body['performer_profile_id'],'neutral_artifact_uri':body['neutral_artifact_uri'],'a':a,'b':b,'rubric_version':body.get('rubric_version','bass-calibration-rubric-v1'),'blinded':body.get('blinded',True)}
            expected=sha256(json.dumps(canonical,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if fingerprint != expected: raise ValueError('Bass calibration trial integrity check failed')
            body['fingerprint']=fingerprint; out.append(body)
        return out
    def judgments(self)->list[dict]:
        out=[]
        if not (self.root/'judgments').exists(): return out
        for p in sorted((self.root/'judgments').glob('*.json')):
            body=json.loads(p.read_text()); raw=json.dumps(body['payload'],sort_keys=True,separators=(',',':'))
            if sha256(raw.encode()).hexdigest()!=body.get('sha256'): raise ValueError('Bass calibration judgment integrity check failed')
            out.append(body['payload'])
        return out
    def summary(self, performer_profile_id:str, *, minimum_trials:int=3, minimum_judgments:int=6, confidence_floor:float=.65, consistency_floor:float=.67)->BassCalibrationSummary:
        trials=[t for t in self.trials() if t.get('performer_profile_id')==performer_profile_id]
        ids={t['trial_id'] for t in trials}; js=[j for j in self.judgments() if j.get('trial_id') in ids]
        blockers=[]
        if len(trials)<minimum_trials: blockers.append(f'minimum_trials:{minimum_trials}')
        if len(js)<minimum_judgments: blockers.append(f'minimum_judgments:{minimum_judgments}')
        mean_conf=sum(float(j['confidence']) for j in js)/len(js) if js else 0.0
        if mean_conf<confidence_floor: blockers.append('confidence_below_floor')
        if js:
            counts={'A':0,'B':0}
            for j in js: counts[str(j['preferred_candidate'])]+=1
            consistency=max(counts.values())/len(js)
            axes=('closer_to_target','better_pocket','better_harmony','better_articulation')
            agreements=[]
            for j in js:
                preferred=str(j['preferred_candidate'])
                agreements.extend(1.0 if str(j[a])==preferred else 0.0 for a in axes)
            rubric_agreement=sum(agreements)/len(agreements) if agreements else 0.0
        else:
            consistency=0.0; rubric_agreement=0.0
        if consistency<consistency_floor: blockers.append('preference_consistency_below_floor')
        if rubric_agreement<consistency_floor: blockers.append('rubric_agreement_below_floor')
        calibration_confidence=mean_conf*consistency*rubric_agreement
        state='calibrated' if not blockers and trials else ('review' if trials else 'blocked')
        return BassCalibrationSummary(performer_profile_id,len(trials),len(js),round(mean_conf,6),round(consistency,6),round(rubric_agreement,6),round(calibration_confidence,6),state,tuple(blockers))

from pathlib import Path
from backend.trackai_platform.source_intake import JsonSourceEvidenceRepository, SourceEvidence

def test_source_evidence_persists_and_review_state_updates(tmp_path: Path):
    repo=JsonSourceEvidenceRepository(tmp_path)
    e=SourceEvidence('bass','s1','p1','file:///x','x','v1',{'tempo_bpm':120},False)
    repo.put(e)
    assert repo.list_for_subject('bass','p1')[0].human_reviewed is False
    repo.mark_human_reviewed('bass','p1','s1',True)
    assert repo.list_for_subject('bass','p1')[0].human_reviewed is True

from pathlib import Path
from backend.trackai_platform.bass_artifacts import BassFeatureArtifact, JsonBassFeatureArtifactStore
from backend.trackai_platform.bass_features import extract_bass_features, normalize_midi_notes

def test_feature_artifact_roundtrip_and_tamper_detection(tmp_path: Path):
    notes=normalize_midi_notes([{"onset_sec":0,"duration_sec":0.1,"midi_note":36,"velocity":0.8}])
    features=extract_bass_features(notes,tempo_bpm=120,kick_onsets_sec=[0])
    artifact=BassFeatureArtifact("s1","p1","file:///bass.wav",features.extractor_version,features)
    store=JsonBassFeatureArtifactStore(tmp_path)
    path=store.put(artifact)
    payload=store.load_payload("s1")
    assert payload["features"]["event_count"]==1
    body=path.read_text().replace('"event_count": 1','"event_count": 2')
    path.write_text(body)
    try:
        store.load_payload("s1")
        assert False, "tamper should fail"
    except ValueError:
        pass

from backend.trackai_platform.bass_features import normalize_midi_notes, normalize_audio_events, extract_bass_features

def test_midi_features_capture_kick_lock_and_harmony():
    notes=normalize_midi_notes([
        {"onset_sec":0.0,"duration_sec":.15,"midi_note":36,"velocity":.8,"articulation":"mute"},
        {"onset_sec":.5,"duration_sec":.15,"midi_note":40,"velocity":.7,"articulation":"normal"},
        {"onset_sec":1.0,"duration_sec":.15,"midi_note":43,"velocity":.8,"articulation":"slide"},
        {"onset_sec":1.5,"duration_sec":.15,"midi_note":36,"velocity":.8,"articulation":"normal"},
    ])
    f=extract_bass_features(notes,tempo_bpm=120,kick_onsets_sec=[0,.5,1,1.5],chord_pitch_classes=[{0,4,7}],chord_index_for_event=[0,0,0,0])
    assert f.kick_lock_score==1.0
    assert f.chord_tone_ratio==1.0
    assert "kick_locked" in f.technique_tags
    assert "muted_or_staccato" in f.technique_tags
    assert "legato_articulation" in f.technique_tags

def test_audio_normalizer_drops_low_confidence_events():
    events=normalize_audio_events([
        {"onset_sec":0,"duration_sec":.4,"midi_note":40,"confidence":.9},
        {"onset_sec":1,"duration_sec":.4,"midi_note":41,"confidence":.2},
    ])
    assert len(events)==1

def test_features_are_deterministic():
    notes=normalize_midi_notes([{"onset_sec":0,"duration_sec":1,"midi_note":40}])
    a=extract_bass_features(notes,tempo_bpm=100)
    b=extract_bass_features(notes,tempo_bpm=100)
    assert a.fingerprint()==b.fingerprint()

def test_features_convert_to_ingestion_observation():
    from backend.trackai_platform.bass_features import observation_from_features
    notes=normalize_midi_notes([{"onset_sec":0,"duration_sec":.1,"midi_note":40,"articulation":"mute"}])
    f=extract_bass_features(notes,tempo_bpm=120,kick_onsets_sec=[0])
    obs=observation_from_features(source_id="s1",performer_profile_id="p1",provenance_uri="s3://evidence/s1",tempo_bpm=120,meter="4/4",features=f)
    assert obs.kick_alignment_score==1.0
    assert obs.note_length_profile=="staccato"
    assert obs.extraction_version=="bass-features-v1"

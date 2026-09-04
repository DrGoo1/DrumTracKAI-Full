from backend.trackai_platform.bass_adapters import analyzed_audio_to_note_events

def test_audio_adapter_normalizes_external_analysis():
    events=analyzed_audio_to_note_events([{"onset_sec":0.25,"duration_sec":0.4,"midi_note":40,"confidence":0.9,"articulation":"slide"}])
    assert len(events)==1
    assert events[0].midi_note==40
    assert events[0].articulation=="slide"

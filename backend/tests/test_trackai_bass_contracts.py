from backend.trackai_platform.bass_contracts import *

def test_source_fingerprint_is_stable():
    x=BassSourceObservation("s1","p1","s3://bucket/source",120,"4/4","E","chords",.82,"short-muted",("mute","slide"),("syncopated",),"bass-extract-v1")
    assert len(x.fingerprint()) == 64
    assert x.fingerprint() == x.fingerprint()

def test_assimilation_defaults_fail_closed():
    p=BassAssimilationProfile("a1","p1",("f",),("muted-funk",),("laid-back",),("approach-tones",),("ghost-notes",))
    assert p.readiness == "blocked"
    assert p.human_review_required is True
    assert p.execution_authorized is False

def test_generation_plan_requires_exact_versions_before_approval():
    try:
        build_bass_plan(plan_id="g",profile_id="a",tempo_bpm=100,meter="4/4",chord_map_id="c",section_map_id="s",kick_events_id="k",role="supportive",density=.5,humanization=.2,requested_articulations=("mute",),approved_for_generation=True)
    except ValueError as exc:
        assert "exact provider" in str(exc)
    else:
        raise AssertionError("expected fail closed")

def test_generation_plan_cannot_authorize_execution():
    try:
        build_bass_plan(plan_id="g",profile_id="a",tempo_bpm=100,meter="4/4",chord_map_id="c",section_map_id="s",kick_events_id="k",role="supportive",density=.5,humanization=.2,requested_articulations=(),execution_authorized=True)
    except ValueError as exc:
        assert "not certified" in str(exc)
    else:
        raise AssertionError("expected fail closed")

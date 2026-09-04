import pytest
from backend.trackai_platform import available_instrument_specs, get_instrument_spec


def test_shared_registry_preserves_drums_and_adds_bass():
    by_id = {item.instrument_id: item for item in available_instrument_specs()}
    assert {"drums", "bass"} <= set(by_id)
    assert by_id["drums"].product_id == "drumtrackai"
    assert by_id["bass"].product_id == "basstrackai"
    assert by_id["bass"].execution_authorized is False


def test_bass_contract_contains_rhythm_harmony_and_articulation_evidence():
    bass = get_instrument_spec("bass")
    rating_keys = {item.key for item in bass.ratings}
    assert {"kick_lock", "note_length", "harmonic_accuracy", "articulation"} <= rating_keys
    assert {"chord_map", "kick_events", "drum_groove"} <= set(bass.conditioning_inputs)


def test_unknown_instrument_fails_closed():
    with pytest.raises(ValueError):
        get_instrument_spec("banjo")

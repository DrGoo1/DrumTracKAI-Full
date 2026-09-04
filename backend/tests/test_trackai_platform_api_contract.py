from backend.trackai_platform import available_instrument_specs


def test_platform_registry_is_shared_and_non_executable():
    specs = available_instrument_specs()
    assert [spec.instrument_id for spec in specs] == ["drums", "bass"]
    assert all(spec.execution_authorized is False for spec in specs)
    bass = next(spec for spec in specs if spec.instrument_id == "bass")
    assert bass.subject_label.startswith("Bassist")
    assert "kick_events" in bass.conditioning_inputs

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "publish_admin_assimilation_to_postgres.py"
    spec = importlib.util.spec_from_file_location("publish_admin_assimilation_to_postgres", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves postponed annotations through sys.modules while the
    # dynamically loaded module is executing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _song(root: Path, drummer: str, song: str) -> Path:
    path = root / drummer / song
    path.mkdir(parents=True, exist_ok=True)
    (path / "drum_analysis.json").write_text("{}", encoding="utf-8")
    return path


def test_discovery_matches_admin_processed_stems_layout(tmp_path: Path) -> None:
    module = _load_module()
    bonham_a = _song(tmp_path, "john_bonham", "song_a")
    bonham_b = _song(tmp_path, "john_bonham", "song_b")
    _song(tmp_path, "neil_peart", "song_a")
    ignored = tmp_path / "john_bonham" / "incomplete"
    ignored.mkdir(parents=True)

    found = module.discover_song_folders(tmp_path, requested_slugs=["john_bonham"])

    assert [item.drummer_slug for item in found] == ["john_bonham", "john_bonham"]
    assert [item.path for item in found] == [bonham_a.resolve(), bonham_b.resolve()]


def test_discovery_rejects_missing_requested_drummer(tmp_path: Path) -> None:
    module = _load_module()
    _song(tmp_path, "john_bonham", "song_a")

    with pytest.raises(module.AssimilationPublishError, match="neil_peart"):
        module.discover_song_folders(tmp_path, requested_slugs=["neil_peart"])


def test_phase_failure_is_fail_closed() -> None:
    module = _load_module()

    assert module._phase_failure("phase5", {"saved": True}) is None
    assert module._phase_failure("phase5", {"saved": False}) == "saved=false"
    assert module._phase_failure("phase5", {"saved": False, "error": "no rollup"}) == "no rollup"
    assert module._phase_failure("phase2", {"processed": 4}) is None
    assert module._phase_failure("phase2", None) == "phase2 returned no result"


def test_safe_phase_summary_excludes_profile_payloads_and_paths() -> None:
    module = _load_module()
    result = module._safe_phase_summary(
        {
            "saved": True,
            "preset_id": "phase6_john_bonham",
            "rollup": {"secret_profile": True},
            "song_folder": "F:/private/youtube/song",
            "source_url": "https://youtube.example/private",
        }
    )

    assert result == {"saved": True, "preset_id": "phase6_john_bonham"}


def test_database_fingerprint_does_not_reveal_url() -> None:
    module = _load_module()
    url = "postgresql://user:password@example.invalid:5432/database"
    fingerprint = module._db_fingerprint(url)

    assert len(fingerprint) == 16
    assert "password" not in fingerprint
    assert "example" not in fingerprint

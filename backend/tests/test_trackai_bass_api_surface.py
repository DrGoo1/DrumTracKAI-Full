from pathlib import Path

def test_bass_admin_routes_exist_and_drum_routes_remain():
    text=(Path(__file__).resolve().parents[1]/'calibration_v2_api.py').read_text()
    assert '@router.post("/admin/platform/bass/sources")' in text
    assert '@router.get("/admin/platform/bass/datasets/{performer_profile_id}")' in text
    assert '@router.post("/admin/platform/bass/datasets/{performer_profile_id}/build-profile")' in text
    assert '@router.get("/admin/drummers/assimilation")' in text
    assert '"execution_authorized":False' in text

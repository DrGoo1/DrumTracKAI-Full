from fastapi.testclient import TestClient

from backend.studiomind_trackai_contract_app import app


def test_contract_host_exposes_only_metadata_boundary(monkeypatch) -> None:
    monkeypatch.setenv("STUDIOMIND_TRACKAI_SANDBOX_AUTH", "test-only-value")
    client = TestClient(app)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {
        "schema_version": "1.1.0",
        "service": "drumtrackai-studiomind-contract",
        "metadata_intake_available": True,
        "generation_authorized": False,
        "artifact_access_authorized": False,
        "daw_execution_authorized": False,
    }

    capability = client.get(
        "/v1/studiomind/capabilities",
        headers={"Authorization": "Bearer test-only-value"},
    )
    assert capability.status_code == 200
    assert capability.json()["metadata_intake_available"] is True
    assert capability.json()["generation_available_through_this_endpoint"] is False

    for forbidden_path in ("/docs", "/redoc", "/openapi.json", "/calibration"):
        assert client.get(forbidden_path).status_code == 404


def test_contract_host_keeps_capabilities_authenticated(monkeypatch) -> None:
    monkeypatch.setenv("STUDIOMIND_TRACKAI_SANDBOX_AUTH", "test-only-value")
    response = TestClient(app).get("/v1/studiomind/capabilities")
    assert response.status_code == 401

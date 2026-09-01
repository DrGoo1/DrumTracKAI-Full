from fastapi.testclient import TestClient

from backend.studiomind_trackai_contract_app import app


def _route_paths(routes) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        nested = getattr(route, "routes", None)
        if nested is not None:
            paths.update(_route_paths(nested))
    return paths


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

    assert _route_paths(app.routes) == {
        "/healthz",
        "/v1/studiomind/capabilities",
        "/v1/studiomind/generation-requests",
    }


def test_contract_host_keeps_capabilities_authenticated(monkeypatch) -> None:
    monkeypatch.setenv("STUDIOMIND_TRACKAI_SANDBOX_AUTH", "test-only-value")
    response = TestClient(app).get("/v1/studiomind/capabilities")
    assert response.status_code == 401

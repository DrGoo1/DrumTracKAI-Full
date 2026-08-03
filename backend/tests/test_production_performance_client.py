import pytest

from backend.services.production_performance_client import (
    ProductionGenerationUnavailable,
    ProductionPerformanceClient,
)


def test_http_mode_fails_closed_without_base_url():
    client = ProductionPerformanceClient(mode="http", base_url="")
    with pytest.raises(ProductionGenerationUnavailable):
        client.generate_performance_spec(
            cfg={"style": "rock"},
            songmap_summary={"sections": []},
            drummer_profile={"drummer_id": "test"},
        )

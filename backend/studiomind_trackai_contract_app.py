"""Minimal ASGI host for the StudioMind/DrumTracKAI contract boundary.

This application mounts metadata validation and non-executing plan preparation.
It does not import calibration, model, database, rendering, or artifact services.
"""
from __future__ import annotations

import os
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from backend.studiomind_trackai_api import SCHEMA_VERSION, router
from backend.studiomind_trackai_generation_api import router as generation_plan_router
from backend.studiomind_trackai_execution_api import router as generation_execution_router


class ContractHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.1.0"] = SCHEMA_VERSION
    service: Literal["drumtrackai-studiomind-contract"] = (
        "drumtrackai-studiomind-contract"
    )
    metadata_intake_available: Literal[True] = True
    generation_plan_preparation_available: Literal[True] = True
    generation_execution_configured: bool = False
    generation_authorized: Literal[False] = False
    artifact_access_authorized: Literal[False] = False
    daw_execution_authorized: Literal[False] = False


def create_contract_app() -> FastAPI:
    application = FastAPI(
        title="DrumTracKAI StudioMind Contract",
        version=SCHEMA_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.get("/healthz", response_model=ContractHealth)
    async def healthz() -> ContractHealth:
        execution_configured = (
            os.getenv("STUDIOMIND_TRACKAI_GENERATION_ENABLED", "").strip().lower()
            == "true"
            and bool(os.getenv("DRUMTRACKAI_GENERATION_API_BASE", "").strip())
            and bool(os.getenv("STUDIOMIND_TRACKAI_REPLAY_DB_PATH", "").strip())
        )
        return ContractHealth(generation_execution_configured=execution_configured)

    application.include_router(router)
    application.include_router(generation_plan_router)
    application.include_router(generation_execution_router)
    return application


app = create_contract_app()


__all__ = ["ContractHealth", "app", "create_contract_app"]

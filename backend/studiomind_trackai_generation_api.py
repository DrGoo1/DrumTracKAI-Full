"""HTTP boundary for non-executing StudioMind generation-plan preparation."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from backend.services.studiomind_trackai_generation import (
    GenerationPlanReceipt,
    GenerationPlanRequest,
    prepare_generation_plan,
)
from backend.studiomind_trackai_api import _require_bearer


router = APIRouter(prefix="/v1/studiomind", tags=["studiomind-trackai"])


@router.post(
    "/generation-plans",
    response_model=GenerationPlanReceipt,
    status_code=status.HTTP_202_ACCEPTED,
)
async def prepare_generation(
    payload: GenerationPlanRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> GenerationPlanReceipt:
    _require_bearer(authorization)
    try:
        return prepare_generation_plan(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


__all__ = ["router"]

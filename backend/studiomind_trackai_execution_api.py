"""Authenticated, explicitly enabled StudioMind drum-generation execution."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from backend.services.studiomind_trackai_execution import (
    ApprovalExpiredError,
    ApprovalReplayError,
    ExecutionBindingError,
    GenerationBackendUnavailable,
    GenerationExecutionReceipt,
    GenerationExecutionRequest,
    build_configured_executor,
)
from backend.studiomind_trackai_api import _require_bearer


router = APIRouter(prefix="/v1/studiomind", tags=["studiomind-trackai"])


@router.post(
    "/generation-executions",
    response_model=GenerationExecutionReceipt,
    status_code=status.HTTP_201_CREATED,
)
async def execute_generation(
    payload: GenerationExecutionRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> GenerationExecutionReceipt:
    _require_bearer(authorization)
    try:
        return build_configured_executor().execute(payload)
    except ExecutionBindingError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ApprovalExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    except ApprovalReplayError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (GenerationBackendUnavailable, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DrumTracKAI generation is unavailable",
        ) from exc


__all__ = ["router"]

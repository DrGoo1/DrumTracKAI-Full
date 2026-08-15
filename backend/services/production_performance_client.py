"""Fail-closed adapter to the production DrumTracKAI performance-spec engine.

Production mode should use HTTP against a service running ``llm_service.app``.
The in-process mode is provided only as a migration bridge and for local tests.
There is deliberately no heuristic or unchanged-event fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Dict, Optional

import requests


class ProductionGenerationUnavailable(RuntimeError):
    """Raised when calibration cannot reach the real production generation engine."""


@dataclass(frozen=True)
class PerformanceSpecResult:
    spec: Dict[str, Any]
    metadata: Dict[str, Any]
    endpoint: str
    engine_mode: str


class ProductionPerformanceClient:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        mode: Optional[str] = None,
    ) -> None:
        self._base_url = str(
            base_url
            or os.getenv("CALIBRATION_GENERATION_API_BASE", "")
            or os.getenv("DRUMTRACKAI_GENERATION_API_BASE", "")
        ).strip().rstrip("/")
        self._timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("CALIBRATION_GENERATION_TIMEOUT_SECONDS", "30")
        )
        requested_mode = str(mode or os.getenv("CALIBRATION_GENERATION_MODE", "")).strip().lower()
        self._mode = requested_mode or ("http" if self._base_url else "inprocess")
        if self._mode not in {"http", "inprocess"}:
            raise ValueError("CALIBRATION_GENERATION_MODE must be 'http' or 'inprocess'")

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def endpoint(self) -> str:
        if self._mode == "http":
            return f"{self._base_url}/v1/performance_spec"
        return "inprocess:llm_service.app.performance_spec"

    def generate_performance_spec(
        self,
        *,
        cfg: Dict[str, Any],
        songmap_summary: Dict[str, Any],
        drummer_profile: Dict[str, Any],
    ) -> PerformanceSpecResult:
        if not isinstance(drummer_profile, dict) or not drummer_profile:
            raise ProductionGenerationUnavailable("Drummer profile is empty")

        payload = {
            "cfg": dict(cfg or {}),
            "songmap_summary": dict(songmap_summary or {}),
            "drummer_profile": dict(drummer_profile or {}),
        }
        if self._mode == "http":
            result = self._generate_http(payload)
        else:
            result = self._generate_inprocess(payload)

        spec = result.get("spec") if isinstance(result, dict) else None
        if not isinstance(spec, dict) or not spec.get("phrases"):
            raise ProductionGenerationUnavailable(
                f"Production performance engine returned no usable phrases via {self.endpoint}"
            )
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        return PerformanceSpecResult(
            spec=spec,
            metadata=metadata,
            endpoint=self.endpoint,
            engine_mode=self._mode,
        )

    def healthcheck(self) -> Dict[str, Any]:
        if self._mode == "inprocess":
            return {"ok": True, "mode": self._mode, "endpoint": self.endpoint}
        if not self._base_url:
            return {"ok": False, "mode": self._mode, "error": "base URL not configured"}
        try:
            response = requests.get(f"{self._base_url}/healthz", timeout=min(8.0, self._timeout))
            return {
                "ok": bool(response.ok),
                "mode": self._mode,
                "endpoint": self.endpoint,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {"ok": False, "mode": self._mode, "endpoint": self.endpoint, "error": str(exc)}

    def _generate_http(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._base_url:
            raise ProductionGenerationUnavailable(
                "CALIBRATION_GENERATION_MODE=http but CALIBRATION_GENERATION_API_BASE is empty"
            )
        try:
            response = requests.post(
                f"{self._base_url}/v1/performance_spec",
                json=payload,
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            raise ProductionGenerationUnavailable(
                f"Could not contact production performance engine: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise ProductionGenerationUnavailable(
                f"Production performance engine returned HTTP {response.status_code}: "
                f"{response.text[:1500]}"
            )
        try:
            data = response.json()
        except Exception as exc:
            raise ProductionGenerationUnavailable(
                f"Production performance engine returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ProductionGenerationUnavailable(f"Production performance engine failed: {data}")
        return data

    @staticmethod
    def _generate_inprocess(payload: Dict[str, Any]) -> Dict[str, Any]:
        # Lazy import is intentional. Importing llm_service.app at module load time
        # would create a cycle because llm_service.app also includes calibration routes.
        try:
            from llm_service.app import PerformanceSpecRequest, performance_spec

            response = performance_spec(
                PerformanceSpecRequest(
                    cfg=payload["cfg"],
                    songmap_summary=payload["songmap_summary"],
                    drummer_profile=payload["drummer_profile"],
                )
            )
            if hasattr(response, "model_dump"):
                data = response.model_dump()
            elif hasattr(response, "dict"):
                data = response.dict()
            elif isinstance(response, dict):
                data = response
            else:
                raise TypeError(f"Unexpected response type: {type(response)!r}")
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise ProductionGenerationUnavailable(f"In-process production engine failed: {data}")
            return data
        except ProductionGenerationUnavailable:
            raise
        except Exception as exc:
            raise ProductionGenerationUnavailable(
                f"In-process production engine could not be invoked: {exc}"
            ) from exc

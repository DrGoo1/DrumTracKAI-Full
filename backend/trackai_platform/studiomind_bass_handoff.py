"""StudioMind-facing BassTracKAI handoff contract."""
from __future__ import annotations
from dataclasses import dataclass
from .bass_contracts import BassGenerationPlan
from .bass_rollup import BassPerformerRollup

@dataclass(frozen=True)
class StudioMindBassHandoff:
    product_id: str
    performer_profile_id: str
    rollup_version: str
    provider_version: str | None
    model_version: str | None
    calibration_state: str
    plan_id: str
    advisory_ready: bool
    execution_authorized: bool=False

def build_studiomind_bass_handoff(plan: BassGenerationPlan, rollup: BassPerformerRollup, *, calibration_state: str) -> StudioMindBassHandoff:
    plan.validate()
    advisory_ready=bool(plan.approved_for_generation and plan.provider_version and plan.model_version and calibration_state=='calibrated')
    return StudioMindBassHandoff('basstrackai',rollup.performer_profile_id,rollup.rollup_version,plan.provider_version,plan.model_version,calibration_state,plan.plan_id,advisory_ready,False)

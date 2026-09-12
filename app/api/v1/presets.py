"""Preset discovery endpoints for BehaviorSim simulation engine."""

from typing import List, Optional
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.services.simulation import get_preset_metadata, list_available_presets

router = APIRouter(prefix="/presets", tags=["presets"])


class PresetResponse(BaseModel):
    """Preset metadata summary schema."""

    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., description="Canonical preset name")
    description: str = Field(..., description="Human-readable description of the domain simulation")
    available: bool = Field(True, description="Whether this preset is ready for simulation")
    default_profile: str = Field(..., description="Default behavioral profile used if none specified")
    supported_profiles: List[str] = Field(..., description="List of supported profiles/cohorts")
    supported_states: List[str] = Field(..., description="List of behavioral state labels modeled")


@router.get(
    "",
    response_model=List[PresetResponse],
    summary="List simulation presets",
    description="Retrieve public catalog of all available simulation presets, supported cohorts, and states.",
)
def list_presets() -> List[PresetResponse]:
    """Return all available simulation presets."""
    return [PresetResponse(**p) for p in list_available_presets()]


@router.get(
    "/{preset}",
    response_model=PresetResponse,
    summary="Get preset details",
    description="Retrieve detailed configuration metadata for a specific simulation preset.",
)
def get_preset(preset: str) -> PresetResponse:
    """Return metadata for a single preset by name or alias."""
    meta = get_preset_metadata(preset)
    return PresetResponse(**meta)

"""Simulation execution endpoint for BehaviorSim API."""

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError
from app.core.rate_limit import check_rate_limit
from app.db.session import get_db
from app.services.simulation import execute_simulation, normalize_preset_name
from app.services.usage import record_usage_result, refund_usage, reserve_usage

logger = logging.getLogger("behaviorsim_api.api.v1.simulations")

router = APIRouter(prefix="/simulations", tags=["simulations"])


class SimulationRequest(BaseModel):
    """Payload schema for requesting a synthetic simulation run."""

    model_config = ConfigDict(extra="forbid")

    preset: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Supported simulation domain preset (e.g., 'education', 'mobile', 'finance', 'healthcare')",
        examples=["education"],
    )
    num_interactions: int = Field(
        ...,
        ge=1,
        le=100000,
        description="Number of behavioral interactions to simulate",
        examples=[100],
    )
    seed: Optional[int] = Field(
        None,
        ge=0,
        le=2147483647,
        description="Optional seed for deterministic reproducibility across runs",
        examples=[42],
    )
    profile: Optional[str] = Field(
        None,
        min_length=1,
        max_length=50,
        description="Optional domain persona/cohort profile name",
        examples=["average"],
    )
    initial_state: Optional[str] = Field(
        None,
        min_length=1,
        max_length=50,
        description="Optional initial behavioral state label",
        examples=["Optimal"],
    )


class SimulationMetadata(BaseModel):
    """Reproducibility and operational provenance metadata for a simulation run."""

    behaviorsim_version: str = Field(..., description="Version of the core behaviorsim package used")
    api_version: str = Field(..., description="Semantic version of BehaviorSim API")
    compute_ms: int = Field(..., description="Total simulation compute time in milliseconds")
    reproducible: bool = Field(
        ...,
        description="True if an explicit seed was supplied guaranteeing identical reproduction",
    )


class SimulationResponse(BaseModel):
    """Successful simulation response containing generated telemetry and run provenance."""

    simulation_id: str = Field(..., description="Unique identifier for the simulation run")
    preset: str = Field(..., description="Canonical preset name used for generation")
    num_interactions: int = Field(..., description="Number of interactions generated")
    seed: Optional[int] = Field(None, description="Random seed used, or null if unseeded")
    data: List[Dict[str, Any]] = Field(..., description="JSON-serialized synthetic interaction records")
    metadata: SimulationMetadata = Field(..., description="Run provenance and reproducibility metadata")


@router.post(
    "",
    response_model=SimulationResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute behavioral simulation",
    description="Authenticate, reserve quota, and execute a behavioral simulation run using the published BehaviorSim engine.",
    dependencies=[Depends(check_rate_limit)],
)
def run_simulation(
    request: SimulationRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SimulationResponse:
    """Execute a validated simulation run within caller's plan limits."""
    user = principal.user
    plan = user.plan
    if plan is None:
        from app.services.plan import get_or_create_free_plan

        plan = get_or_create_free_plan(db)
        user.plan = plan

    # 1. Per-request interaction limit check against user plan
    if request.num_interactions > plan.max_interactions_per_request:
        logger.warning(
            "Interaction limit exceeded: user_id=%s requested=%s limit=%s",
            user.id,
            request.num_interactions,
            plan.max_interactions_per_request,
        )
        raise BehaviorSimAPIError(
            message=f"Requested interactions ({request.num_interactions}) exceeds your plan limit of {plan.max_interactions_per_request}.",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={
                "code": "interaction_limit_exceeded",
                "requested": request.num_interactions,
                "max_allowed": plan.max_interactions_per_request,
            },
        )

    api_key_id = principal.api_key.id if principal.api_key else None

    # 2. Atomically reserve quota (raises 429 quota_exceeded if exhausted)
    reserve_usage(
        db=db,
        user=user,
        requested_interactions=request.num_interactions,
        delta_requests=1,
        api_key_id=api_key_id,
    )

    # 3. Execute BehaviorSim simulation via service adapter
    try:
        simulation_id, records, compute_ms = execute_simulation(
            preset=request.preset,
            num_interactions=request.num_interactions,
            seed=request.seed,
            profile=request.profile,
            initial_state=request.initial_state,
        )
    except BehaviorSimAPIError:
        refund_usage(
            db=db,
            user=user,
            requested_interactions=request.num_interactions,
            delta_requests=1,
        )
        record_usage_result(
            db=db,
            user=user,
            event_type="simulation_failed",
            success=False,
            interaction_count=0,
            compute_ms=0,
            api_key_id=api_key_id,
        )
        raise
    except Exception as exc:
        # Refund reserved quota on unexpected internal generation failure
        refund_usage(
            db=db,
            user=user,
            requested_interactions=request.num_interactions,
            delta_requests=1,
        )
        record_usage_result(
            db=db,
            user=user,
            event_type="simulation_failed",
            success=False,
            interaction_count=0,
            compute_ms=0,
            api_key_id=api_key_id,
        )
        raise BehaviorSimAPIError(
            message="Internal simulation generation failed. Please try again or contact support.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details={"code": "simulation_generation_failed"},
        ) from exc

    # 4. Record successful usage completion event
    record_usage_result(
        db=db,
        user=user,
        event_type="simulation_completed",
        success=True,
        interaction_count=request.num_interactions,
        compute_ms=compute_ms,
        api_key_id=api_key_id,
    )

    canonical_preset = normalize_preset_name(request.preset)

    return SimulationResponse(
        simulation_id=simulation_id,
        preset=canonical_preset,
        num_interactions=len(records),
        seed=request.seed,
        data=records,
        metadata=SimulationMetadata(
            behaviorsim_version="1.0.1",
            api_version=get_settings().API_VERSION,
            compute_ms=compute_ms,
            reproducible=(request.seed is not None),
        ),
    )

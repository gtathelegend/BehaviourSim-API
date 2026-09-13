import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Path as FastPath, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError
from app.core.rate_limit import check_rate_limit
from app.db.models.simulation import Simulation
from app.db.session import get_db
from app.services.simulation import (
    PRESET_ALIASES,
    SUPPORTED_PRESETS,
    execute_simulation,
    normalize_preset_name,
)
from app.services.usage import record_usage_result, refund_usage, reserve_usage

ALLOWED_SIMULATION_STATUSES = {"completed", "failed", "pending"}

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


class SimulationDetailResponse(BaseModel):
    """Durable simulation record detail response schema."""

    simulation_id: str = Field(..., description="Unique identifier for the simulation run")
    preset: str = Field(..., description="Canonical preset name used for generation")
    num_interactions: int = Field(..., description="Number of interactions generated")
    seed: Optional[int] = Field(None, description="Random seed used, or null if unseeded")
    profile: Optional[str] = Field(None, description="Domain persona/cohort profile name used")
    initial_state: Optional[str] = Field(None, description="Initial behavioral state used")
    status: str = Field(..., description="Simulation execution status (e.g. 'completed')")
    data: List[Dict[str, Any]] = Field(..., description="JSON-serialized synthetic interaction records")
    metadata: SimulationMetadata = Field(..., description="Run provenance and reproducibility metadata")
    created_at: datetime = Field(..., description="Timestamp when the simulation was created")
    completed_at: datetime = Field(..., description="Timestamp when the simulation completed")


class SimulationHistoryItem(BaseModel):
    """Lightweight simulation metadata item for history listings (excludes interaction data)."""

    simulation_id: str = Field(..., description="Unique identifier for the simulation run")
    preset: str = Field(..., description="Canonical preset name used for generation")
    num_interactions: int = Field(..., description="Number of interactions generated")
    seed: Optional[int] = Field(None, description="Random seed used, or null if unseeded")
    profile: Optional[str] = Field(None, description="Domain persona/cohort profile name used")
    initial_state: Optional[str] = Field(None, description="Initial behavioral state used")
    status: str = Field(..., description="Simulation execution status (e.g. 'completed')")
    compute_ms: int = Field(..., description="Total simulation compute time in milliseconds")
    reproducible: bool = Field(..., description="True if an explicit seed was supplied guaranteeing reproducibility")
    behaviorsim_version: str = Field(..., description="Version of the core behaviorsim package used")
    api_version: str = Field(..., description="Semantic version of BehaviorSim API")
    created_at: datetime = Field(..., description="Timestamp when the simulation was created")
    completed_at: datetime = Field(..., description="Timestamp when the simulation completed")


class SimulationHistoryResponse(BaseModel):
    """Bounded paginated simulation history response."""

    items: List[SimulationHistoryItem] = Field(..., description="List of simulation run metadata items")
    page: int = Field(..., description="Current page number (1-indexed)")
    page_size: int = Field(..., description="Maximum items per page")
    total: int = Field(..., description="Total number of simulations matching filters for caller")
    has_next: bool = Field(..., description="True if subsequent pages exist")


@router.post(
    "",
    response_model=SimulationResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute behavioral simulation",
    description="Authenticate, reserve quota, execute a behavioral simulation, and persist the run and results.",
    dependencies=[Depends(check_rate_limit)],
)
def run_simulation(
    request: SimulationRequest,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SimulationResponse:
    """Execute a validated simulation run within caller's plan limits and persist the results."""
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
        simulation_id_str, records, compute_ms = execute_simulation(
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

    canonical_preset = normalize_preset_name(request.preset)
    sim_uuid = uuid.UUID(simulation_id_str)
    settings = get_settings()
    user_id = user.id

    # 4. Persist simulation record and bounded results in database
    try:
        sim_record = Simulation(
            id=sim_uuid,
            user_id=user_id,
            preset=canonical_preset,
            num_interactions=len(records),
            seed=request.seed,
            profile=request.profile,
            initial_state=request.initial_state,
            status="completed",
            result_storage="database",
            data=records,
            behaviorsim_version="1.0.1",
            api_version=settings.API_VERSION,
            compute_ms=compute_ms,
            reproducible=(request.seed is not None),
        )
        db.add(sim_record)
        db.commit()
    except Exception as exc:
        logger.error("Failed to persist simulation record: %s", exc, exc_info=True)
        # Refund quota on persistence failure to guarantee consistency
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
            message="Failed to persist simulation run. Quota has been refunded.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details={"code": "simulation_persistence_failed"},
        ) from exc

    # 5. Record successful usage completion event
    record_usage_result(
        db=db,
        user=user,
        event_type="simulation_completed",
        success=True,
        interaction_count=request.num_interactions,
        compute_ms=compute_ms,
        api_key_id=api_key_id,
    )

    return SimulationResponse(
        simulation_id=simulation_id_str,
        preset=canonical_preset,
        num_interactions=len(records),
        seed=request.seed,
        data=records,
        metadata=SimulationMetadata(
            behaviorsim_version="1.0.1",
            api_version=settings.API_VERSION,
            compute_ms=compute_ms,
            reproducible=(request.seed is not None),
        ),
    )


@router.get(
    "",
    response_model=SimulationHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="List simulation history",
    description="List historical simulation runs owned by the authenticated caller with pagination and bounded filtering.",
    dependencies=[Depends(check_rate_limit)],
)
def list_simulations(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page (max 100)"),
    preset: Optional[str] = Query(None, min_length=1, max_length=50, description="Optional filter by preset domain"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        min_length=1,
        max_length=20,
        description="Optional filter by simulation status",
    ),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SimulationHistoryResponse:
    """Retrieve paginated simulation history for the current authenticated user."""
    # 1. Base ownership filter (strict caller scoping)
    clauses = [Simulation.user_id == principal.user.id]

    # 2. Preset filter validation & normalization
    if preset is not None:
        normalized_preset = normalize_preset_name(preset)
        if normalized_preset not in SUPPORTED_PRESETS:
            allowed = sorted(list(SUPPORTED_PRESETS.keys()) + list(PRESET_ALIASES.keys()))
            raise BehaviorSimAPIError(
                message=f"Preset '{preset}' is not recognized. Supported presets: {allowed}",
                status_code=status.HTTP_400_BAD_REQUEST,
                details={"code": "invalid_preset", "allowed_presets": allowed},
            )
        clauses.append(Simulation.preset == normalized_preset)

    # 3. Status filter validation
    if status_filter is not None:
        cleaned_status = status_filter.strip().lower()
        if cleaned_status not in ALLOWED_SIMULATION_STATUSES:
            allowed_statuses = sorted(list(ALLOWED_SIMULATION_STATUSES))
            raise BehaviorSimAPIError(
                message=f"Status '{status_filter}' is not recognized. Allowed statuses: {allowed_statuses}",
                status_code=status.HTTP_400_BAD_REQUEST,
                details={"code": "invalid_status", "allowed_statuses": allowed_statuses},
            )
        clauses.append(Simulation.status == cleaned_status)

    # 4. Total count query
    count_stmt = select(func.count(Simulation.id)).where(*clauses)
    total = db.execute(count_stmt).scalar() or 0

    # 5. Metadata projection query (excluding heavy JSONB data)
    stmt = (
        select(
            Simulation.id,
            Simulation.preset,
            Simulation.num_interactions,
            Simulation.seed,
            Simulation.profile,
            Simulation.initial_state,
            Simulation.status,
            Simulation.compute_ms,
            Simulation.reproducible,
            Simulation.behaviorsim_version,
            Simulation.api_version,
            Simulation.created_at,
            Simulation.completed_at,
        )
        .where(*clauses)
        .order_by(Simulation.created_at.desc(), Simulation.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = db.execute(stmt).all()

    items = [
        SimulationHistoryItem(
            simulation_id=str(row.id),
            preset=row.preset,
            num_interactions=row.num_interactions,
            seed=row.seed,
            profile=row.profile,
            initial_state=row.initial_state,
            status=row.status,
            compute_ms=row.compute_ms,
            reproducible=row.reproducible,
            behaviorsim_version=row.behaviorsim_version,
            api_version=row.api_version,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]

    has_next = (page * page_size) < total

    return SimulationHistoryResponse(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        has_next=has_next,
    )


@router.get(
    "/{simulation_id}",
    response_model=SimulationDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve persisted simulation run",
    description="Retrieve execution details, reproducibility metadata, and generated results of a simulation owned by the caller.",
)
def get_simulation(
    simulation_id: str = FastPath(..., description="UUID of the simulation run to retrieve"),
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> SimulationDetailResponse:
    """Retrieve a persisted simulation run with strict owner-level IDOR enforcement."""
    # 1. Validate UUID format
    try:
        sim_uuid = uuid.UUID(simulation_id)
    except (ValueError, TypeError):
        raise BehaviorSimAPIError(
            message=f"Simulation '{simulation_id}' not found.",
            status_code=status.HTTP_404_NOT_FOUND,
            details={"code": "simulation_not_found"},
        )

    # 2. Query simulation strictly matching both simulation.id AND owner user_id
    stmt = select(Simulation).where(
        Simulation.id == sim_uuid,
        Simulation.user_id == principal.user.id,
    )
    sim = db.execute(stmt).scalar_one_or_none()

    if sim is None:
        raise BehaviorSimAPIError(
            message=f"Simulation '{simulation_id}' not found.",
            status_code=status.HTTP_404_NOT_FOUND,
            details={"code": "simulation_not_found"},
        )

    return SimulationDetailResponse(
        simulation_id=str(sim.id),
        preset=sim.preset,
        num_interactions=sim.num_interactions,
        seed=sim.seed,
        profile=sim.profile,
        initial_state=sim.initial_state,
        status=sim.status,
        data=sim.data,
        metadata=SimulationMetadata(
            behaviorsim_version=sim.behaviorsim_version,
            api_version=sim.api_version,
            compute_ms=sim.compute_ms,
            reproducible=sim.reproducible,
        ),
        created_at=sim.created_at,
        completed_at=sim.completed_at,
    )

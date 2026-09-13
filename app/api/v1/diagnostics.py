"""Operational diagnostics and telemetry endpoints."""

import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.metrics import operational_metrics
from app.db.models.simulation import Simulation
from app.db.session import get_db
from app.services.simulation_job import STATUS_PENDING, STATUS_RUNNING

logger = logging.getLogger("behaviorsim_api.api.v1.diagnostics")

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


class QueueStatus(BaseModel):
    """Queue backlog and active worker execution counts."""

    pending: int
    running: int


class DiagnosticsResponse(BaseModel):
    """Safe read-only operational telemetry and diagnostics response."""

    status: str
    version: str
    queue: QueueStatus
    metrics: Dict[str, Any]


@router.get(
    "",
    response_model=DiagnosticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Operational diagnostics and metrics",
    description="Retrieve safe read-only operational metrics, queue depth, and service telemetry. Exposes no secrets or user data.",
)
def get_diagnostics(db: Session = Depends(get_db)) -> DiagnosticsResponse:
    """Return operational metrics summary and current queue backlog."""
    settings = get_settings()

    # Query current queue depth safely
    pending_count = (
        db.execute(
            select(func.count(Simulation.id)).where(Simulation.status == STATUS_PENDING)
        ).scalar()
        or 0
    )
    running_count = (
        db.execute(
            select(func.count(Simulation.id)).where(Simulation.status == STATUS_RUNNING)
        ).scalar()
        or 0
    )

    metrics_summary = operational_metrics.get_summary()

    return DiagnosticsResponse(
        status="ok",
        version=settings.API_VERSION,
        queue=QueueStatus(pending=pending_count, running=running_count),
        metrics=metrics_summary,
    )

import logging
import threading
import time
from typing import Any, Dict, Tuple
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

# Thread-safe in-memory cache for queue depth to protect DB from rapid polling
_queue_cache_lock = threading.Lock()
_queue_cache_time: float = 0.0
_queue_cached_counts: Tuple[int, int] = (0, 0)
QUEUE_CACHE_TTL_SECONDS = 5.0


def reset_diagnostics_cache() -> None:
    """Reset cached queue metrics (useful for testing)."""
    global _queue_cache_time, _queue_cached_counts
    with _queue_cache_lock:
        _queue_cache_time = 0.0
        _queue_cached_counts = (0, 0)


def get_cached_queue_counts(db: Session, ttl: float = QUEUE_CACHE_TTL_SECONDS) -> Tuple[int, int]:
    """Retrieve queue counts with in-memory TTL caching to prevent DB connection exhaustion."""
    global _queue_cache_time, _queue_cached_counts
    now = time.time()
    with _queue_cache_lock:
        if (now - _queue_cache_time) < ttl:
            return _queue_cached_counts

    # Query fresh counts outside lock
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

    with _queue_cache_lock:
        _queue_cached_counts = (pending_count, running_count)
        _queue_cache_time = now

    return pending_count, running_count


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

    pending_count, running_count = get_cached_queue_counts(db)
    metrics_summary = operational_metrics.get_summary()

    return DiagnosticsResponse(
        status="ok",
        version=settings.API_VERSION,
        queue=QueueStatus(pending=pending_count, running=running_count),
        metrics=metrics_summary,
    )

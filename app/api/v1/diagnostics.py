"""Operational diagnostics and metrics API endpoint."""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
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

# Thread-safe in-memory cache for queue depth and ages to protect DB from rapid polling
_queue_cache_lock = threading.Lock()
_queue_cache_time: float = 0.0
_queue_cached_data: Tuple[int, int, int, Optional[float], Optional[float]] = (0, 0, 0, None, None)
QUEUE_CACHE_TTL_SECONDS = 5.0


def reset_diagnostics_cache() -> None:
    """Reset cached queue metrics (useful for testing)."""
    global _queue_cache_time, _queue_cached_data
    with _queue_cache_lock:
        _queue_cache_time = 0.0
        _queue_cached_data = (0, 0, 0, None, None)


def get_cached_queue_metrics(
    db: Session, ttl: float = QUEUE_CACHE_TTL_SECONDS
) -> Tuple[int, int, int, Optional[float], Optional[float]]:
    """Retrieve queue counts and oldest job ages with in-memory TTL caching.

    Returns:
        (pending_count, running_count, queue_depth, oldest_pending_age_seconds, oldest_running_age_seconds)
    """
    global _queue_cache_time, _queue_cached_data
    now = time.time()
    with _queue_cache_lock:
        if (now - _queue_cache_time) < ttl:
            return _queue_cached_data

    now_utc = datetime.now(timezone.utc)

    # Query pending count and oldest pending created_at (index-backed)
    p_res = db.execute(
        select(
            func.count(Simulation.id),
            func.min(Simulation.created_at),
        ).where(Simulation.status == STATUS_PENDING)
    ).one_or_none()

    pending_count = (p_res[0] or 0) if p_res else 0
    oldest_pending_dt = p_res[1] if p_res else None

    # Query running count and oldest running started_at (index-backed)
    r_res = db.execute(
        select(
            func.count(Simulation.id),
            func.min(Simulation.started_at),
        ).where(Simulation.status == STATUS_RUNNING)
    ).one_or_none()

    running_count = (r_res[0] or 0) if r_res else 0
    oldest_running_dt = r_res[1] if r_res else None

    # Compute ages safely
    oldest_pending_age: Optional[float] = None
    if oldest_pending_dt is not None:
        p_dt = oldest_pending_dt if oldest_pending_dt.tzinfo else oldest_pending_dt.replace(tzinfo=timezone.utc)
        oldest_pending_age = max(0.0, round((now_utc - p_dt).total_seconds(), 2))

    oldest_running_age: Optional[float] = None
    if oldest_running_dt is not None:
        r_dt = oldest_running_dt if oldest_running_dt.tzinfo else oldest_running_dt.replace(tzinfo=timezone.utc)
        oldest_running_age = max(0.0, round((now_utc - r_dt).total_seconds(), 2))

    depth = pending_count + running_count
    cached_result = (pending_count, running_count, depth, oldest_pending_age, oldest_running_age)

    with _queue_cache_lock:
        _queue_cached_data = cached_result
        _queue_cache_time = now

    return cached_result


def get_cached_queue_counts(
    db: Session, ttl: float = QUEUE_CACHE_TTL_SECONDS
) -> Tuple[int, int]:
    """Retrieve (pending, running) counts with in-memory TTL caching (backwards compatibility)."""
    pending, running, _, _, _ = get_cached_queue_metrics(db, ttl=ttl)
    return pending, running


class QueueStatus(BaseModel):
    """Queue backlog, active worker execution counts, and job wait age metrics."""

    pending: int
    running: int
    depth: int
    oldest_pending_age_seconds: Optional[float] = None
    oldest_running_age_seconds: Optional[float] = None


class HealthIndicators(BaseModel):
    """Operational SLO and warning indicators derived from real-time telemetry."""

    queue_healthy: bool
    cleanup_healthy: bool
    error_rate_pct: float


class DiagnosticsResponse(BaseModel):
    """Safe read-only operational telemetry and diagnostics response."""

    status: str
    version: str
    queue: QueueStatus
    health: HealthIndicators
    metrics: Dict[str, Any]


@router.get(
    "",
    response_model=DiagnosticsResponse,
    status_code=status.HTTP_200_OK,
    summary="Operational diagnostics and metrics",
    description="Retrieve safe read-only operational metrics, queue depth, job age, and service telemetry. Exposes no secrets or user data.",
)
def get_diagnostics(db: Session = Depends(get_db)) -> DiagnosticsResponse:
    """Return operational metrics summary, current queue backlog, and SLO health indicators."""
    settings = get_settings()

    (
        pending_count,
        running_count,
        depth,
        oldest_pending_age,
        oldest_running_age,
    ) = get_cached_queue_metrics(db)

    metrics_summary = operational_metrics.get_summary()

    # Derived health indicators based on established operational warning thresholds
    queue_healthy = (depth < 50) and (oldest_pending_age is None or oldest_pending_age < 60.0)
    cleanup_stale = metrics_summary.get("retention", {}).get("stale_warning", False)
    cleanup_healthy = not cleanup_stale

    total_reqs = metrics_summary.get("api", {}).get("requests_total", 0)
    server_errors = metrics_summary.get("api", {}).get("requests_by_status_class", {}).get("5xx", 0)
    error_rate_pct = round((server_errors / total_reqs * 100.0), 2) if total_reqs > 0 else 0.0

    return DiagnosticsResponse(
        status="ok",
        version=settings.API_VERSION,
        queue=QueueStatus(
            pending=pending_count,
            running=running_count,
            depth=depth,
            oldest_pending_age_seconds=oldest_pending_age,
            oldest_running_age_seconds=oldest_running_age,
        ),
        health=HealthIndicators(
            queue_healthy=queue_healthy,
            cleanup_healthy=cleanup_healthy,
            error_rate_pct=error_rate_pct,
        ),
        metrics=metrics_summary,
    )

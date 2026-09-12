"""Health and readiness probe endpoints."""

import logging
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db

logger = logging.getLogger("behaviorsim_api.routes.health")

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Liveness probe response schema."""

    status: str
    version: str


class ReadinessResponse(BaseModel):
    """Readiness probe response schema."""

    status: str
    database: str
    version: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Process liveness check",
    description="Lightweight liveness probe checking that the application process is running. Performs no external calls.",
)
async def health_check() -> HealthResponse:
    """Return health status of the API process."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version=settings.API_VERSION,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Service readiness probe",
    description="Readiness probe verifying that the service can accept traffic by validating database connectivity.",
)
def readiness_check(
    response: Response,
    db: Session = Depends(get_db),
) -> ReadinessResponse:
    """Check database connectivity to determine traffic readiness."""
    settings = get_settings()
    try:
        db.execute(text("SELECT 1"))
        return ReadinessResponse(
            status="ready",
            database="connected",
            version=settings.API_VERSION,
        )
    except Exception as exc:
        logger.error("Readiness check failed: database unavailable (%s)", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="not_ready",
            database="unavailable",
            version=settings.API_VERSION,
        )

"""Health check endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Return health status of the API process."""
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version=settings.API_VERSION,
    )

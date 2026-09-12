"""API v1 router organization."""

from fastapi import APIRouter

# Base router for all v1 endpoints (simulations, profiles, etc. in Phase 1+)
api_v1_router = APIRouter()

# Future sub-routers will be included here:
# api_v1_router.include_router(simulations.router, prefix="/simulations", tags=["simulations"])

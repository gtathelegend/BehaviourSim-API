"""API v1 router organization."""

from fastapi import APIRouter

# Base router for all v1 endpoints (simulations, profiles, etc. in Phase 1+)
api_v1_router = APIRouter()

from app.api.v1.account import router as account_router
from app.api.v1.auth import router as auth_router
from app.api.v1.usage import router as usage_router

api_v1_router.include_router(auth_router)
api_v1_router.include_router(account_router)
api_v1_router.include_router(usage_router)

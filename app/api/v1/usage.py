"""Usage and quota accounting API endpoint."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.core.rate_limit import check_rate_limit
from app.db.session import get_db
from app.services.usage import get_user_usage_summary

router = APIRouter(tags=["usage"])


class PlanSummary(BaseModel):
    name: str
    monthly_requests: int
    monthly_interactions: int
    max_interactions_per_request: int
    requests_per_minute: int
    max_concurrent_simulations: int


class PeriodSummary(BaseModel):
    start: str
    end: str


class UsageMetrics(BaseModel):
    requests: int
    interactions: int


class UsageResponse(BaseModel):
    plan: PlanSummary
    period: PeriodSummary
    usage: UsageMetrics
    remaining: UsageMetrics


@router.get("/usage", response_model=UsageResponse, dependencies=[Depends(check_rate_limit)])
async def get_usage(
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> UsageResponse:
    """Retrieve current usage, plan entitlements, and remaining quota for authenticated user."""
    summary = get_user_usage_summary(db=db, user=principal.user)
    return UsageResponse(**summary)

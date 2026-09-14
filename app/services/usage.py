"""Usage accounting and atomic quota reservation service."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import BehaviorSimAPIError
from app.db.models.usage import MonthlyUsage, UsageEvent
from app.db.models.user import User

logger = logging.getLogger("behaviorsim_api.services.usage")


def get_current_period_start(target_date: Optional[datetime] = None) -> datetime:
    """Calculate the UTC start boundary (first day at 00:00:00) for the usage month."""
    dt = target_date or datetime.now(timezone.utc)
    return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=timezone.utc)


def get_current_period_end(target_date: Optional[datetime] = None) -> datetime:
    """Calculate the UTC end boundary (first day of next month at 00:00:00)."""
    dt = target_date or datetime.now(timezone.utc)
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return datetime(dt.year, dt.month + 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def get_or_create_monthly_usage(
    db: Session,
    user_id: uuid.UUID,
    period_start: datetime,
    for_update: bool = False,
) -> MonthlyUsage:
    """Retrieve or initialize the monthly usage row for a user in the given period.

    Uses row-level locking (FOR UPDATE) when for_update=True to serialize concurrent access.
    """
    stmt = select(MonthlyUsage).where(
        MonthlyUsage.user_id == user_id,
        MonthlyUsage.period_start == period_start,
    )
    if for_update:
        stmt = stmt.with_for_update()

    usage = db.scalars(stmt).first()
    if usage:
        return usage

    # Initialize new monthly usage record
    new_usage = MonthlyUsage(
        id=uuid.uuid4(),
        user_id=user_id,
        period_start=period_start,
        request_count=0,
        interaction_count=0,
    )
    db.add(new_usage)
    try:
        db.commit()
        db.refresh(new_usage)
        return new_usage
    except IntegrityError:
        # Concurrent creation race condition occurred
        db.rollback()
        stmt = select(MonthlyUsage).where(
            MonthlyUsage.user_id == user_id,
            MonthlyUsage.period_start == period_start,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return db.scalars(stmt).first()


def reserve_usage(
    db: Session,
    user: User,
    requested_interactions: int = 0,
    delta_requests: int = 1,
    api_key_id: Optional[uuid.UUID] = None,
    request_id: Optional[str] = None,
) -> MonthlyUsage:
    """Atomically reserve requests and interactions against the user's plan quota.

    Enforcement Steps:
    1. Validate requested_interactions <= plan.max_interactions_per_request.
    2. Acquire atomic row-level lock on the current month's usage record.
    3. Check remaining monthly request and interaction quotas.
    4. If quotas are respected, increment usage counters and record audit UsageEvent.
    5. Commit atomically to guarantee zero oversubscription under concurrency.
    """
    plan = user.plan
    if plan is None:
        from app.services.plan import get_or_create_free_plan

        plan = get_or_create_free_plan(db)
        user.plan = plan

    # 1. Per-request interaction limit check
    if requested_interactions > plan.max_interactions_per_request:
        logger.warning(
            "Per-request limit exceeded: user_id=%s requested %s interactions, max allowed %s",
            user.id,
            requested_interactions,
            plan.max_interactions_per_request,
        )
        raise BehaviorSimAPIError(
            message=f"Requested interaction count ({requested_interactions}) exceeds your plan's single-request limit of {plan.max_interactions_per_request}.",
            status_code=400,
            details={
                "code": "interaction_limit_exceeded",
                "max_allowed": plan.max_interactions_per_request,
                "requested": requested_interactions,
            },
        )

    # 2. Acquire atomic lock on monthly usage record
    period_start = get_current_period_start()
    usage = get_or_create_monthly_usage(db, user.id, period_start, for_update=True)

    # 3. Quota boundary enforcement via atomic conditional SQL update
    stmt = (
        update(MonthlyUsage)
        .where(
            MonthlyUsage.id == usage.id,
            MonthlyUsage.request_count + delta_requests <= plan.monthly_requests,
            MonthlyUsage.interaction_count + requested_interactions <= plan.monthly_interactions,
        )
        .values(
            request_count=MonthlyUsage.request_count + delta_requests,
            interaction_count=MonthlyUsage.interaction_count + requested_interactions,
            updated_at=datetime.now(timezone.utc),
        )
    )
    result = db.execute(stmt)
    if result.rowcount == 0:
        db.refresh(usage)
        from app.core.metrics import operational_metrics
        operational_metrics.record_quota_exhausted()
        if usage.request_count + delta_requests > plan.monthly_requests:
            logger.info(
                "Monthly request quota exhausted: user_id=%s current=%s delta=%s limit=%s",
                user.id,
                usage.request_count,
                delta_requests,
                plan.monthly_requests,
            )
            raise BehaviorSimAPIError(
                message=f"Monthly request quota exhausted ({usage.request_count}/{plan.monthly_requests}).",
                status_code=429,
                details={
                    "code": "quota_exceeded",
                    "resource": "requests",
                    "current": usage.request_count,
                    "limit": plan.monthly_requests,
                },
            )
        else:
            logger.info(
                "Monthly interaction quota exhausted: user_id=%s current=%s requested=%s limit=%s",
                user.id,
                usage.interaction_count,
                requested_interactions,
                plan.monthly_interactions,
            )
            raise BehaviorSimAPIError(
                message=f"Monthly interaction quota exhausted ({usage.interaction_count}/{plan.monthly_interactions}).",
                status_code=429,
                details={
                    "code": "quota_exceeded",
                    "resource": "interactions",
                    "current": usage.interaction_count,
                    "limit": plan.monthly_interactions,
                },
            )

    # 4. Commit atomic reservation and record usage event
    event = UsageEvent(
        id=uuid.uuid4(),
        user_id=user.id,
        api_key_id=api_key_id,
        event_type="simulation_request",
        request_id=request_id,
        interaction_count=requested_interactions,
        success=True,
    )
    db.add(event)
    db.commit()
    db.refresh(usage)

    logger.info(
        "Reserved usage for user_id=%s: requests=%s/%s, interactions=%s/%s",
        user.id,
        usage.request_count,
        plan.monthly_requests,
        usage.interaction_count,
        plan.monthly_interactions,
    )
    return usage


def record_usage_result(
    db: Session,
    user: Optional[User] = None,
    event_type: str = "",
    success: bool = True,
    interaction_count: int = 0,
    compute_ms: Optional[int] = None,
    api_key_id: Optional[uuid.UUID] = None,
    request_id: Optional[str] = None,
    metadata_json: Optional[str] = None,
    user_id: Optional[uuid.UUID] = None,
) -> UsageEvent:
    """Record an operational usage event outcome (success/failure) for audit logs."""
    target_user_id = user_id or (user.id if user else None)
    event = UsageEvent(
        id=uuid.uuid4(),
        user_id=target_user_id,
        api_key_id=api_key_id,
        event_type=event_type,
        request_id=request_id,
        interaction_count=interaction_count,
        compute_ms=compute_ms,
        success=success,
        metadata_json=metadata_json,
    )
    db.add(event)
    db.commit()
    return event


def get_user_usage_summary(db: Session, user: User) -> Dict[str, Any]:
    """Retrieve full plan, usage, remaining quota, and billing period for a user."""
    plan = user.plan
    if plan is None:
        from app.services.plan import get_or_create_free_plan

        plan = get_or_create_free_plan(db)

    period_start = get_current_period_start()
    period_end = get_current_period_end()
    usage = get_or_create_monthly_usage(db, user.id, period_start)

    remaining_requests = max(0, plan.monthly_requests - usage.request_count)
    remaining_interactions = max(0, plan.monthly_interactions - usage.interaction_count)

    return {
        "plan": {
            "name": plan.name,
            "monthly_requests": plan.monthly_requests,
            "monthly_interactions": plan.monthly_interactions,
            "max_interactions_per_request": plan.max_interactions_per_request,
            "requests_per_minute": plan.requests_per_minute,
            "max_concurrent_simulations": plan.max_concurrent_simulations,
        },
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
        },
        "usage": {
            "requests": usage.request_count,
            "interactions": usage.interaction_count,
        },
        "remaining": {
            "requests": remaining_requests,
            "interactions": remaining_interactions,
        },
    }


def refund_usage(
    db: Session,
    user: Optional[User] = None,
    requested_interactions: int = 0,
    delta_requests: int = 1,
    *,
    user_id: Optional[uuid.UUID] = None,
) -> None:
    """Atomically decrement usage counters when simulation execution fails after quota reservation."""
    target_user_id = user_id or (user.id if user else None)
    if not target_user_id:
        return

    period_start = get_current_period_start()
    stmt = (
        update(MonthlyUsage)
        .where(
            MonthlyUsage.user_id == target_user_id,
            MonthlyUsage.period_start == period_start,
        )
        .values(
            request_count=case(
                (MonthlyUsage.request_count >= delta_requests, MonthlyUsage.request_count - delta_requests),
                else_=0,
            ),
            interaction_count=case(
                (
                    MonthlyUsage.interaction_count >= requested_interactions,
                    MonthlyUsage.interaction_count - requested_interactions,
                ),
                else_=0,
            ),
            updated_at=datetime.now(timezone.utc),
        )
    )
    db.execute(stmt)
    db.commit()
    logger.info(
        "Refunded usage for user_id=%s: -%s requests, -%s interactions",
        target_user_id,
        delta_requests,
        requested_interactions,
    )

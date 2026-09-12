"""Tests for plan entitlements, monthly usage calculation, quota reservation, and API-key limits."""

from datetime import datetime, timezone
import pytest
from sqlalchemy.orm import Session

from app.core.errors import BehaviorSimAPIError
from app.db.models.user import User
from app.services.api_key import create_api_key, revoke_api_key
from app.services.plan import FREE_PLAN_DEFAULTS, get_or_create_free_plan
from app.services.usage import (
    get_current_period_end,
    get_current_period_start,
    reserve_usage,
)


def test_free_plan_defaults(db_session: Session):
    """Verify default free plan has expected entitlements."""
    plan = get_or_create_free_plan(db_session)
    assert plan.name == "free"
    assert plan.monthly_requests == FREE_PLAN_DEFAULTS["monthly_requests"]
    assert plan.monthly_interactions == FREE_PLAN_DEFAULTS["monthly_interactions"]
    assert plan.max_interactions_per_request == FREE_PLAN_DEFAULTS["max_interactions_per_request"]
    assert plan.requests_per_minute == FREE_PLAN_DEFAULTS["requests_per_minute"]
    assert plan.max_concurrent_simulations == FREE_PLAN_DEFAULTS["max_concurrent_simulations"]
    assert plan.max_api_keys == FREE_PLAN_DEFAULTS["max_api_keys"]
    assert plan.calibration_enabled is False
    assert plan.large_exports_enabled is False


def test_user_creation_associates_free_plan(db_session: Session):
    """Verify new user automatically inherits the default free plan."""
    user = User(email="plan_user@example.com", display_name="Plan User")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    assert user.plan is not None
    assert user.plan.name == "free"


def test_period_boundaries_calculation():
    """Verify UTC period start and end boundaries calculation."""
    test_date = datetime(2026, 9, 15, 14, 30, 0, tzinfo=timezone.utc)
    start = get_current_period_start(test_date)
    end = get_current_period_end(test_date)

    assert start == datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Year rollover case
    dec_date = datetime(2026, 12, 25, 10, 0, 0, tzinfo=timezone.utc)
    dec_start = get_current_period_start(dec_date)
    dec_end = get_current_period_end(dec_date)
    assert dec_start == datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert dec_end == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_single_request_interaction_limit(db_session: Session):
    """Verify per-request interaction limit is enforced independently of monthly quota."""
    user = User(email="interaction_limit_user@example.com")
    db_session.add(user)
    db_session.commit()

    # Plan max is 1000. 1000 must succeed.
    usage = reserve_usage(db_session, user, requested_interactions=1000, delta_requests=1)
    assert usage.interaction_count == 1000

    # 1001 must fail with 400 Bad Request
    with pytest.raises(BehaviorSimAPIError) as exc_info:
        reserve_usage(db_session, user, requested_interactions=1001, delta_requests=1)

    assert exc_info.value.status_code == 400
    assert exc_info.value.details.get("code") == "interaction_limit_exceeded"


def test_monthly_request_quota_boundary(db_session: Session):
    """Verify monthly request quota enforcement (100 allowed, 101st fails)."""
    user = User(email="request_quota_user@example.com")
    db_session.add(user)
    db_session.commit()

    # Simulate 99 requests
    reserve_usage(db_session, user, requested_interactions=1, delta_requests=99)

    # 100th request must succeed
    usage = reserve_usage(db_session, user, requested_interactions=1, delta_requests=1)
    assert usage.request_count == 100

    # 101st request must fail with 429
    with pytest.raises(BehaviorSimAPIError) as exc_info:
        reserve_usage(db_session, user, requested_interactions=1, delta_requests=1)

    assert exc_info.value.status_code == 429
    assert exc_info.value.details.get("code") == "quota_exceeded"
    assert exc_info.value.details.get("resource") == "requests"


def test_monthly_interaction_quota_boundary(db_session: Session):
    """Verify monthly interaction quota enforcement (10000 allowed, oversubscription fails)."""
    user = User(email="interaction_quota_user@example.com")
    db_session.add(user)
    db_session.commit()

    # Reserve 10 x 1000 interactions = 10000 interactions
    for _ in range(10):
        reserve_usage(db_session, user, requested_interactions=1000, delta_requests=1)

    # Next reservation for even 1 interaction must fail with 429
    with pytest.raises(BehaviorSimAPIError) as exc_info:
        reserve_usage(db_session, user, requested_interactions=1, delta_requests=1)

    assert exc_info.value.status_code == 429
    assert exc_info.value.details.get("code") == "quota_exceeded"
    assert exc_info.value.details.get("resource") == "interactions"


def test_api_key_plan_limit_enforcement(db_session: Session):
    """Verify user cannot exceed active API-key quota (free plan allows 1 key)."""
    user = User(email="key_limit_user@example.com")
    db_session.add(user)
    db_session.commit()

    # 1st key creation succeeds
    key1 = create_api_key(db_session, user, name="Key 1")
    assert key1.id is not None

    # 2nd key creation fails with 403 Forbidden
    with pytest.raises(BehaviorSimAPIError) as exc_info:
        create_api_key(db_session, user, name="Key 2")

    assert exc_info.value.status_code == 403
    assert exc_info.value.details.get("code") == "plan_limit_exceeded"

    # Revoking key 1 allows creating a new key
    revoke_api_key(db_session, user, key1.id)
    key2 = create_api_key(db_session, user, name="Key 2 After Revoke")
    assert key2.id is not None

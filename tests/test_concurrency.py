"""Concurrency tests verifying atomic quota reservation under simultaneous requests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.errors import BehaviorSimAPIError
from app.db.models import Base
from app.db.models.plan import Plan
from app.db.models.user import User
from app.services.usage import (
    get_current_period_start,
    get_or_create_monthly_usage,
    reserve_usage,
)


def test_concurrent_quota_reservations_prevent_oversubscription(tmp_path: Path):
    """Verify concurrent requests cannot oversubscribe quota limits."""
    db_file = tmp_path / "concurrency_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30.0},
    )

    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.execute(text("PRAGMA busy_timeout=15000;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionLocal() as db_session:
        # Create custom plan with strict limit of 5 requests
        strict_plan = Plan(
            name="strict_test_plan",
            monthly_requests=5,
            monthly_interactions=5000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=1,
            max_api_keys=5,
        )
        db_session.add(strict_plan)
        db_session.commit()

        user = User(email="concurrent_user@example.com", plan_id=strict_plan.id)
        db_session.add(user)
        db_session.commit()
        user_id = user.id

    success_count = 0
    quota_exceeded_count = 0
    errors = []

    def attempt_reservation():
        nonlocal success_count, quota_exceeded_count
        with SessionLocal() as thread_session:
            t_user = thread_session.get(User, user_id)
            try:
                reserve_usage(
                    db=thread_session,
                    user=t_user,
                    requested_interactions=10,
                    delta_requests=1,
                )
                success_count += 1
            except BehaviorSimAPIError as exc:
                if exc.details.get("code") == "quota_exceeded":
                    quota_exceeded_count += 1
                else:
                    errors.append(exc)
            except Exception as e:
                errors.append(e)

    # Dispatch 15 simultaneous reservation attempts (limit is 5)
    num_threads = 15
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(attempt_reservation) for _ in range(num_threads)]
        for f in futures:
            f.result()

    assert not errors, f"Unexpected errors during concurrent reservation: {errors}"
    assert success_count == 5, f"Expected exactly 5 successes, got {success_count}"
    assert quota_exceeded_count == 10, f"Expected exactly 10 quota_exceeded rejections, got {quota_exceeded_count}"

    # Verify final usage record in database
    with SessionLocal() as verify_session:
        period_start = get_current_period_start()
        final_usage = get_or_create_monthly_usage(verify_session, user_id, period_start)
        assert final_usage.request_count == 5
        assert final_usage.interaction_count == 50

    engine.dispose()

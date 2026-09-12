"""Concurrency tests verifying simulation requests cannot oversubscribe quota."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.rate_limit import default_rate_limiter
from app.db.models import Base
from app.db.models.plan import Plan
from app.db.models.usage import MonthlyUsage
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services.api_key import create_api_key
from app.services.usage import get_current_period_start


def test_concurrent_simulation_quota_enforcement(tmp_path: Path):
    """Verify concurrent simulation requests enforce atomic quota and prevent oversubscription."""
    default_rate_limiter.reset()

    db_file = tmp_path / "sim_concurrency.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30.0},
    )

    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.execute(text("PRAGMA busy_timeout=15000;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as init_session:
        # Create custom plan with strict limit of 3 requests
        strict_plan = Plan(
            name="strict_sim_plan",
            monthly_requests=3,
            monthly_interactions=3000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=5,
            max_api_keys=5,
        )
        init_session.add(strict_plan)
        init_session.commit()

        user = User(email="concurrent_sim_user@example.com", plan_id=strict_plan.id)
        init_session.add(user)
        init_session.commit()

        created_key = create_api_key(init_session, user, name="Concurrency Key")
        api_key_str = created_key.key
        user_id = user.id

    def override_db():
        session = SessionClass()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    try:
        def run_sim_request():
            thread_client = TestClient(app)
            response = thread_client.post(
                "/v1/simulations",
                headers={"Authorization": f"Bearer {api_key_str}"},
                json={"preset": "education", "num_interactions": 10, "seed": 42},
            )
            return response.status_code, response.json()

        # Dispatch 10 concurrent simulation requests
        num_threads = 10
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_sim_request) for _ in range(num_threads)]
            results = [f.result() for f in futures]
    finally:
        app.dependency_overrides.pop(get_db, None)

    status_codes = [status_code for status_code, _ in results]
    successes = status_codes.count(200)
    quota_exhausted = status_codes.count(429)

    assert successes == 3, f"Expected exactly 3 successful simulations, got {successes} (statuses: {status_codes})"
    assert quota_exhausted == 7, f"Expected exactly 7 rejections with 429, got {quota_exhausted}"

    # Verify final usage record in database
    with SessionClass() as verify_session:
        period_start = get_current_period_start()
        usage = verify_session.query(MonthlyUsage).filter_by(user_id=user_id, period_start=period_start).first()
        assert usage is not None
        assert usage.request_count == 3
        assert usage.interaction_count == 30

    engine.dispose()
    default_rate_limiter.reset()

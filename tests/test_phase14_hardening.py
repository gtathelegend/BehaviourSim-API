"""Tests for Phase 14: Reliability, Resource Protection & Concurrency Hardening."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.concurrency import ConcurrencyLimiter, default_concurrency_limiter
from app.core.rate_limit import InMemoryRateLimiter, default_rate_limiter
from app.db.models import Base
from app.db.models.plan import Plan
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services.api_key import create_api_key


def test_concurrency_limiter_unit():
    """Verify ConcurrencyLimiter unit behavior: acquire, limit rejection, release, reset."""
    limiter = ConcurrencyLimiter()
    u1 = str(uuid.uuid4())

    # First acquisition allowed (limit 2)
    assert limiter.acquire(u1, limit=2) is True
    assert limiter.get_active_count(u1) == 1

    # Second acquisition allowed
    assert limiter.acquire(u1, limit=2) is True
    assert limiter.get_active_count(u1) == 2

    # Third acquisition rejected
    assert limiter.acquire(u1, limit=2) is False
    assert limiter.get_active_count(u1) == 2

    # Release one slot
    rem = limiter.release(u1)
    assert rem == 1
    assert limiter.get_active_count(u1) == 1

    # Now acquisition allowed again
    assert limiter.acquire(u1, limit=2) is True
    assert limiter.get_active_count(u1) == 2

    # Release all
    limiter.release(u1)
    rem = limiter.release(u1)
    assert rem == 0
    assert limiter.get_active_count(u1) == 0
    # Entry cleaned up
    assert uuid.UUID(u1) not in limiter._active_counts

    # Re-release on non-existent key is safe
    rem = limiter.release(u1)
    assert rem == 0

    # Reset
    limiter.acquire(u1, limit=5)
    limiter.reset()
    assert len(limiter._active_counts) == 0


def test_rate_limiter_memory_hygiene():
    """Verify InMemoryRateLimiter prunes expired keys and handles opportunistic sweep."""
    limiter = InMemoryRateLimiter()

    # Record entries for multiple keys
    limiter.check_rate_limit("user_a", limit=10, window_seconds=1)
    limiter.check_rate_limit("user_b", limit=10, window_seconds=1)

    assert "user_a" in limiter._records
    assert "user_b" in limiter._records

    # Manually backdate timestamps to simulate expiration
    limiter._records["user_a"] = [time.time() - 10]
    limiter._records["user_b"] = [time.time() - 10]

    # Explicit prune
    pruned = limiter.prune_expired(window_seconds=1)
    assert pruned == 2
    assert "user_a" not in limiter._records
    assert "user_b" not in limiter._records

    # Test opportunistic sweep during check_rate_limit
    limiter._records["stale_user"] = [time.time() - 100]
    limiter._sweep_counter = 99  # Next call will trigger sweep
    limiter.check_rate_limit("fresh_user", limit=10, window_seconds=5)

    assert "stale_user" not in limiter._records
    assert "fresh_user" in limiter._records
    assert limiter._sweep_counter == 0


def test_request_body_size_ceiling_middleware():
    """Verify RequestBodyLimitMiddleware rejects requests exceeding 1MB with 413."""
    client = TestClient(app)

    # Health check is small -> 200
    res = client.get("/health")
    assert res.status_code == 200
    assert "x-request-id" in res.headers

    # Oversized payload (simulate 2MB content-length header)
    headers = {"Content-Length": str(2 * 1024 * 1024), "Content-Type": "application/json"}
    res = client.post("/v1/simulations", content=b"{}", headers=headers)
    assert res.status_code == 413
    data = res.json()
    assert data["error"]["details"]["code"] == "payload_too_large"
    assert "exceeds maximum allowed size" in data["error"]["message"]
    assert "x-request-id" in res.headers


def test_concurrent_simulation_limit_enforced(tmp_path: Path):
    """Verify that a user cannot exceed plan.max_concurrent_simulations."""
    default_rate_limiter.reset()
    default_concurrency_limiter.reset()

    db_file = tmp_path / "sim_concurrency_limit.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        # Plan allows max 1 concurrent simulation
        plan1 = Plan(
            name="single_sim_plan",
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=1,
            max_api_keys=5,
        )
        session.add(plan1)
        session.commit()

        user1 = User(email="user1_conc@example.com", is_active=True, plan_id=plan1.id)
        user2 = User(email="user2_conc@example.com", is_active=True, plan_id=plan1.id)
        session.add_all([user1, user2])
        session.commit()

        key1 = create_api_key(session, user1, name="Key 1").key
        key2 = create_api_key(session, user2, name="Key 2").key

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)

        # Pre-acquire the slot for user1 using default_concurrency_limiter
        with SessionClass() as s:
            db_user1 = s.query(User).filter(User.email == "user1_conc@example.com").first()
            user1_id = db_user1.id

        allowed = default_concurrency_limiter.acquire(user1_id, limit=1)
        assert allowed is True
        assert default_concurrency_limiter.get_active_count(user1_id) == 1

        # Now an API request from user1 should be rejected with 429
        res = client.post(
            "/v1/simulations",
            headers={"Authorization": f"Bearer {key1}"},
            json={
                "preset": "education",
                "num_interactions": 10,
            },
        )
        assert res.status_code == 429
        data = res.json()
        assert data["error"]["details"]["code"] == "concurrent_simulation_limit_exceeded"
        assert "Concurrent simulation limit exceeded" in data["error"]["message"]
        assert "Retry-After" in res.headers

        # But user2 (different user) CAN run a simulation concurrently
        res2 = client.post(
            "/v1/simulations",
            headers={"Authorization": f"Bearer {key2}"},
            json={
                "preset": "education",
                "num_interactions": 10,
            },
        )
        assert res2.status_code == 200

        # Release user1's slot
        default_concurrency_limiter.release(user1_id)

        # Now user1 can run a simulation
        res3 = client.post(
            "/v1/simulations",
            headers={"Authorization": f"Bearer {key1}"},
            json={
                "preset": "education",
                "num_interactions": 10,
            },
        )
        assert res3.status_code == 200

        # Slot was automatically released after simulation finished
        assert default_concurrency_limiter.get_active_count(user1_id) == 0

    finally:
        app.dependency_overrides.clear()
        default_concurrency_limiter.reset()
        default_rate_limiter.reset()


def test_simulation_concurrency_release_on_error(tmp_path: Path):
    """Verify that concurrency slot is released even when simulation execution raises an error."""
    default_rate_limiter.reset()
    default_concurrency_limiter.reset()

    db_file = tmp_path / "sim_release_on_err.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(
            name="err_plan",
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=1,
            max_api_keys=5,
        )
        session.add(plan)
        session.commit()

        user = User(email="err_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        key = create_api_key(session, user, name="Key").key
        user_id = user.id

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)

        # Mock execute_simulation to raise an unexpected RuntimeError
        with patch("app.api.v1.simulations.execute_simulation", side_effect=RuntimeError("Sim crashed")):
            res = client.post(
                "/v1/simulations",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "preset": "education",
                    "num_interactions": 10,
                },
            )
            assert res.status_code == 500

        # Verify the concurrency slot was released in the finally block
        assert default_concurrency_limiter.get_active_count(user_id) == 0

    finally:
        app.dependency_overrides.clear()
        default_concurrency_limiter.reset()
        default_rate_limiter.reset()


def test_api_keys_endpoint_rate_limited(tmp_path: Path):
    """Verify that /v1/api-keys router enforces plan rate limiting."""
    default_rate_limiter.reset()

    db_file = tmp_path / "api_keys_rate.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        # Plan allows only 2 requests per minute
        plan = Plan(
            name="strict_rate_plan",
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=2,
            max_concurrent_simulations=5,
            max_api_keys=5,
        )
        session.add(plan)
        session.commit()

        user = User(email="rate_key_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        key = create_api_key(session, user, name="Key").key

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)

        # Request 1 -> 200
        r1 = client.get("/v1/api-keys", headers={"Authorization": f"Bearer {key}"})
        assert r1.status_code == 200

        # Request 2 -> 200
        r2 = client.get("/v1/api-keys", headers={"Authorization": f"Bearer {key}"})
        assert r2.status_code == 200

        # Request 3 -> 429 Too Many Requests
        r3 = client.get("/v1/api-keys", headers={"Authorization": f"Bearer {key}"})
        assert r3.status_code == 429
        data = r3.json()
        assert data["error"]["details"]["code"] == "rate_limit_exceeded"
        assert "Retry-After" in r3.headers

    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()


def test_concurrent_api_key_creation_serialized(tmp_path: Path):
    """Verify concurrent create_api_key calls are serialized and enforce max_api_keys limit."""
    db_file = tmp_path / "api_key_conc.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.execute(text("PRAGMA busy_timeout=15000;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        # Plan allows max 2 API keys
        plan = Plan(
            name="two_key_plan",
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=5,
            max_api_keys=2,
        )
        session.add(plan)
        session.commit()

        user = User(email="concurrent_key_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        user_id = user.id

    success_count = 0
    limit_reached_count = 0
    errors = []

    def attempt_create(idx: int):
        nonlocal success_count, limit_reached_count
        with SessionClass() as session:
            try:
                db_user = session.query(User).filter(User.id == user_id).first()
                create_api_key(session, db_user, name=f"Key {idx}")
                success_count += 1
            except Exception as e:
                if "Active API key limit reached" in str(e):
                    limit_reached_count += 1
                else:
                    errors.append(str(e))

    # Attempt 5 simultaneous creations
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(attempt_create, i) for i in range(5)]
        for f in futures:
            f.result()

    # Exactly 2 should succeed, 3 should fail with limit reached, 0 unexpected errors
    assert len(errors) == 0, f"Unexpected errors: {errors}"
    assert success_count == 2
    assert limit_reached_count == 3

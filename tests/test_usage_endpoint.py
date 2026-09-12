"""Tests for /v1/usage endpoint and cross-user isolation."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.service import create_user_session
from app.core.config import get_settings
from app.core.rate_limit import default_rate_limiter
from app.db.models.user import User
from app.services.api_key import create_api_key
from app.services.usage import reserve_usage


def test_usage_endpoint_with_api_key(client: TestClient, db_session: Session):
    """Verify GET /v1/usage returns correct usage and remaining quotas using API key."""
    default_rate_limiter.reset()

    user = User(email="usage_api_user@example.com")
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Usage Key")

    # Reserve 1 request and 150 interactions
    reserve_usage(db_session, user, requested_interactions=150, delta_requests=1)

    response = client.get("/v1/usage", headers={"Authorization": f"Bearer {created_key.key}"})
    assert response.status_code == 200

    data = response.json()
    assert data["plan"]["name"] == "free"
    assert data["plan"]["monthly_requests"] == 100
    assert data["plan"]["monthly_interactions"] == 10000
    assert "start" in data["period"]
    assert "end" in data["period"]

    assert data["usage"]["requests"] == 1
    assert data["usage"]["interactions"] == 150
    assert data["remaining"]["requests"] == 99
    assert data["remaining"]["interactions"] == 9850


def test_usage_endpoint_with_session_cookie(client: TestClient, db_session: Session):
    """Verify GET /v1/usage works via browser session cookie."""
    default_rate_limiter.reset()
    settings = get_settings()

    user = User(email="usage_session_user@example.com")
    db_session.add(user)
    db_session.commit()

    _, raw_token = create_user_session(db_session, user)

    client.cookies.set(settings.AUTH_SESSION_COOKIE_NAME, raw_token)
    response = client.get("/v1/usage")
    assert response.status_code == 200
    assert response.json()["plan"]["name"] == "free"


def test_usage_cross_user_isolation(client: TestClient, db_session: Session):
    """Verify strict tenant isolation: User A cannot observe User B's usage."""
    default_rate_limiter.reset()

    user_a = User(email="user_a@example.com")
    user_b = User(email="user_b@example.com")
    db_session.add_all([user_a, user_b])
    db_session.commit()

    key_a = create_api_key(db_session, user_a, name="Key A")
    key_b = create_api_key(db_session, user_b, name="Key B")

    # User A consumes 50 requests and 5,000 interactions (5 batches of 10 requests, 1,000 interactions)
    for _ in range(5):
        reserve_usage(db_session, user_a, requested_interactions=1000, delta_requests=10)

    # Query as User A
    resp_a = client.get("/v1/usage", headers={"Authorization": f"Bearer {key_a.key}"})
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["usage"]["requests"] == 50
    assert data_a["remaining"]["requests"] == 50
    assert data_a["remaining"]["interactions"] == 5000

    # Query as User B (must be 0 consumed, 100 remaining)
    resp_b = client.get("/v1/usage", headers={"Authorization": f"Bearer {key_b.key}"})
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["usage"]["requests"] == 0
    assert data_b["remaining"]["requests"] == 100
    assert data_b["remaining"]["interactions"] == 10000


def test_account_endpoint_exposes_plan(client: TestClient, db_session: Session):
    """Verify GET /v1/account includes the user's plan name."""
    default_rate_limiter.reset()

    user = User(email="account_plan_user@example.com")
    db_session.add(user)
    db_session.commit()

    key = create_api_key(db_session, user, name="Account Plan Key")
    resp = client.get("/v1/account", headers={"Authorization": f"Bearer {key.key}"})
    assert resp.status_code == 200
    assert resp.json()["plan"] == "free"

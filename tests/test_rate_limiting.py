"""Tests for rate limiting implementation, headers, and reset behavior."""

import time
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.rate_limit import InMemoryRateLimiter, default_rate_limiter
from app.db.models.user import User
from app.services.api_key import create_api_key


def test_in_memory_rate_limiter_unit():
    """Verify InMemoryRateLimiter sliding window tracking and retry_after calculation."""
    limiter = InMemoryRateLimiter()
    key = "test_user_key"

    # Allow 5 requests
    for i in range(5):
        allowed, retry_after = limiter.check_rate_limit(key, limit=5, window_seconds=60)
        assert allowed is True
        assert retry_after == 0

    # 6th request must be rejected
    allowed, retry_after = limiter.check_rate_limit(key, limit=5, window_seconds=60)
    assert allowed is False
    assert retry_after > 0

    # Reset clears timestamps
    limiter.reset()
    allowed, retry_after = limiter.check_rate_limit(key, limit=5, window_seconds=60)
    assert allowed is True


def test_rate_limit_endpoint_integration(client: TestClient, db_session: Session):
    """Verify rate-limited endpoint returns HTTP 429 and Retry-After header on 6th request."""
    default_rate_limiter.reset()

    user = User(email="ratelimit_user@example.com")
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Rate Limit Key")
    headers = {"Authorization": f"Bearer {created_key.key}"}

    # 5 requests must succeed (HTTP 200)
    for _ in range(5):
        resp = client.get("/v1/usage", headers=headers)
        assert resp.status_code == 200

    # 6th request must be rejected with 429
    rate_limited_resp = client.get("/v1/usage", headers=headers)
    assert rate_limited_resp.status_code == 429
    assert "Retry-After" in rate_limited_resp.headers
    data = rate_limited_resp.json()
    assert data["error"]["details"]["code"] == "rate_limit_exceeded"

    default_rate_limiter.reset()

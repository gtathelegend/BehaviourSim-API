"""Tests for Phase 6 production hardening, security headers, CORS, request IDs, and readiness."""

from unittest.mock import patch
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models.user import User
from app.services.api_key import create_api_key
from app.services.usage import get_current_period_start, get_or_create_monthly_usage, refund_usage


def test_security_headers_present(client: TestClient):
    """Verify standard security headers are injected into HTTP responses."""
    resp = client.get("/health")
    assert resp.status_code == 200
    headers = resp.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in headers.get("Content-Security-Policy", "")


def test_cors_policy_enforcement(client: TestClient):
    """Verify CORS allowlist: configured origins allowed, unknown origins rejected."""
    # 1. Allowed origin
    resp_allowed = client.options(
        "/v1/presets",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp_allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"

    # 2. Disallowed untrusted origin
    resp_disallowed = client.options(
        "/v1/presets",
        headers={
            "Origin": "http://untrusted-external-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp_disallowed.headers.get("access-control-allow-origin") != "http://untrusted-external-site.com"


def test_request_id_generation_and_propagation(client: TestClient):
    """Verify request correlation IDs are generated or propagated and returned in headers."""
    # 1. Generated when absent
    resp_gen = client.get("/health")
    assert resp_gen.status_code == 200
    req_id = resp_gen.headers.get("X-Request-ID")
    assert req_id is not None
    assert uuid.UUID(req_id)  # Valid UUID format

    # 2. Propagated when safe caller ID supplied
    custom_id = "trace-client-id-abc123"
    resp_prop = client.get("/health", headers={"X-Request-ID": custom_id})
    assert resp_prop.status_code == 200
    assert resp_prop.headers.get("X-Request-ID") == custom_id

    # 3. Replaced with generated UUID when caller supplies unsafe characters
    unsafe_id = "bad;header\r\nvalue<script>"
    resp_unsafe = client.get("/health", headers={"X-Request-ID": unsafe_id})
    assert resp_unsafe.status_code == 200
    sanitized_id = resp_unsafe.headers.get("X-Request-ID")
    assert sanitized_id != unsafe_id
    assert uuid.UUID(sanitized_id)


def test_error_handlers_include_request_id_and_hide_traces(client: TestClient, db_session: Session):
    """Verify validation and internal errors format uniformly with request_id and no stack traces."""
    user = User(email="error_handler_test@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Error Test Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # 1. Pydantic 422 error
    resp_val = client.post("/v1/simulations", headers=headers, json={"preset": "education", "num_interactions": -5})
    assert resp_val.status_code == 422
    data_val = resp_val.json()
    assert "error" in data_val
    assert data_val["error"]["status_code"] == 422
    assert data_val["error"]["request_id"] is not None

    # 2. Unhandled 500 error sanitization
    client_safe = TestClient(client.app, raise_server_exceptions=False)
    with patch("app.api.routes.health.get_settings", side_effect=RuntimeError("Secret database connection string: user:pass@host")):
        resp_500 = client_safe.get("/health")
        assert resp_500.status_code == 500
        data_500 = resp_500.json()
        assert data_500["error"]["status_code"] == 500
        assert data_500["error"]["details"]["code"] == "internal_server_error"
        # Must not expose the secret or stack trace in the response
        assert "Secret database connection string" not in resp_500.text
        assert "RuntimeError" not in resp_500.text
        assert data_500["error"]["request_id"] is not None


def test_readiness_probe(client: TestClient):
    """Verify /ready probe reports 200 when database is healthy and 503 when unhealthy."""
    # 1. Healthy database check
    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["database"] == "connected"

    # 2. Unhealthy database check
    with patch("sqlalchemy.orm.Session.execute", side_effect=OperationalError("connection failure", None, None)):
        resp_down = client.get("/ready")
        assert resp_down.status_code == 503
        data_down = resp_down.json()
        assert data_down["status"] == "not_ready"
        assert data_down["database"] == "unavailable"


def test_negative_refund_prevention(db_session: Session):
    """Verify refunding usage cannot cause usage counters to become negative."""
    user = User(email="refund_invariant_user@example.com")
    db_session.add(user)
    db_session.commit()

    period_start = get_current_period_start()
    usage = get_or_create_monthly_usage(db_session, user.id, period_start)
    assert usage.request_count == 0
    assert usage.interaction_count == 0

    # Attempt to refund 50 requests and 5000 interactions on a 0-balance record
    refund_usage(db_session, user, requested_interactions=5000, delta_requests=50)

    db_session.refresh(usage)
    assert usage.request_count == 0, f"Expected 0, got {usage.request_count}"
    assert usage.interaction_count == 0, f"Expected 0, got {usage.interaction_count}"


def test_production_settings_validation():
    """Verify Settings.validate_production_configuration enforces production invariants."""
    # 1. Development mode ignores production checks
    dev_settings = Settings(APP_ENV="development", DATABASE_URL="sqlite:///:memory:")
    dev_settings.validate_production_configuration()  # Should not raise

    # 2. Production with sqlite must fail
    with pytest.raises(ValueError, match="DATABASE_URL must point to an external production database"):
        bad_db_settings = Settings(
            APP_ENV="production",
            DATABASE_URL="sqlite:///:memory:",
            API_BASE_URL="https://api.behaviorsim.com",
            WEB_BASE_URL="https://app.behaviorsim.com",
            CORS_ORIGINS=["https://app.behaviorsim.com"],
        )
        bad_db_settings.validate_production_configuration()

    # 3. Production with HTTP instead of HTTPS must fail
    with pytest.raises(ValueError, match="API_BASE_URL must use HTTPS in production"):
        bad_url_settings = Settings(
            APP_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:pass@db.example.com:5432/behaviorsim",
            API_BASE_URL="http://api.behaviorsim.com",
            WEB_BASE_URL="https://app.behaviorsim.com",
            CORS_ORIGINS=["https://app.behaviorsim.com"],
        )
        bad_url_settings.validate_production_configuration()

    # 4. Production with wildcard CORS must fail
    with pytest.raises(ValueError, match="CORS origin '\\*' is unsafe for production"):
        bad_cors_settings = Settings(
            APP_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:pass@db.example.com:5432/behaviorsim",
            API_BASE_URL="https://api.behaviorsim.com",
            WEB_BASE_URL="https://app.behaviorsim.com",
            CORS_ORIGINS=["*"],
        )
        bad_cors_settings.validate_production_configuration()


def test_api_key_matching_prefix_wrong_secret_fails(client: TestClient, db_session: Session):
    """Verify that a known valid prefix with an incorrect secret fails authentication."""
    user = User(email="prefix_security_user@example.com")
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Real Key")
    # Take real prefix, but append a fake secret
    fake_key = f"{created_key.key_prefix}fake_secret_that_does_not_match_hash"

    resp = client.get("/v1/account", headers={"Authorization": f"Bearer {fake_key}"})
    assert resp.status_code == 401

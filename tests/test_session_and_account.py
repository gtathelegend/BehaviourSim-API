"""Tests for application sessions, account endpoint, and logout behavior."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.service import create_user_session, validate_session_token
from app.core.config import get_settings
from app.db.models.auth_identity import AuthIdentity
from app.db.models.user import User
from app.services.api_key import create_api_key


def test_session_lifecycle(db_session: Session):
    """Verify session creation, hashing, expiration, and validation."""
    user = User(email="session_user@example.com", is_active=True)
    db_session.add(user)
    db_session.commit()

    session, raw_token = create_user_session(db_session, user)
    assert raw_token.startswith("bs_sess_")
    assert session.token_hash != raw_token

    # 1. Valid token validation
    validated = validate_session_token(db_session, raw_token)
    assert validated is not None
    assert validated.id == session.id

    # 2. Expired session rejected
    session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()
    assert validate_session_token(db_session, raw_token) is None


def test_account_endpoint_with_session_cookie(client: TestClient, db_session: Session):
    """Verify GET /v1/account with valid session cookie."""
    settings = get_settings()

    user = User(email="cookie_account@example.com", display_name="Cookie User", is_active=True)
    db_session.add(user)
    db_session.commit()

    ident = AuthIdentity(user_id=user.id, provider="google", provider_subject="sub_google_123")
    db_session.add(ident)
    db_session.commit()

    _, raw_token = create_user_session(db_session, user)

    client.cookies.set(settings.AUTH_SESSION_COOKIE_NAME, raw_token)
    response = client.get("/v1/account")
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == str(user.id)
    assert data["email"] == "cookie_account@example.com"
    assert data["display_name"] == "Cookie User"
    assert data["is_active"] is True
    assert data["authentication_methods"] == ["google"]

    # Verify sensitive attributes are absent
    assert "token_hash" not in data
    assert "password" not in data
    assert "api_key" not in data


def test_account_endpoint_with_api_key(client: TestClient, db_session: Session):
    """Verify GET /v1/account with developer API key in Authorization header."""
    user = User(email="apikey_account@example.com", display_name="API User", is_active=True)
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Dev Key")

    # Clear cookies to ensure authentication relies on API key
    client.cookies.clear()
    response = client.get("/v1/account", headers={"Authorization": f"Bearer {created_key.key}"})
    assert response.status_code == 200

    data = response.json()
    assert data["id"] == str(user.id)
    assert data["email"] == "apikey_account@example.com"
    assert "api_key" in data["authentication_methods"]


def test_account_endpoint_unauthenticated(client: TestClient):
    """Verify GET /v1/account without credentials returns 401."""
    client.cookies.clear()
    response = client.get("/v1/account")
    assert response.status_code == 401


def test_logout_invalidates_session_and_preserves_api_keys(client: TestClient, db_session: Session):
    """Verify logout invalidates session, clears cookie, while API keys remain unaffected."""
    settings = get_settings()

    user = User(email="logout_test@example.com", is_active=True)
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Persistent Key")
    session, raw_token = create_user_session(db_session, user)

    # 1. Set session cookie and perform logout
    client.cookies.set(settings.AUTH_SESSION_COOKIE_NAME, raw_token)
    logout_resp = client.post("/v1/auth/logout")
    assert logout_resp.status_code == 200
    assert logout_resp.json()["status"] == "ok"

    # Verify session is marked revoked in database
    db_session.refresh(session)
    assert session.revoked_at is not None

    # 2. Subsequent request with revoked session fails
    acc_resp = client.get("/v1/account")
    assert acc_resp.status_code == 401

    # 3. Developer API key remains independently valid
    key_resp = client.get("/v1/account", headers={"Authorization": f"Bearer {created_key.key}"})
    assert key_resp.status_code == 200
    assert key_resp.json()["email"] == "logout_test@example.com"

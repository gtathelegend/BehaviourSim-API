"""Tests for OAuth initiation, state generation, and callback handling."""

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.auth.providers.base import ProviderIdentity
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError


def test_oauth_initiation_google(client: TestClient):
    """Verify Google OAuth initiation sets state cookie and returns redirect."""
    response = client.get("/v1/auth/google", follow_redirects=False)
    assert response.status_code == 307

    location = response.headers.get("location")
    assert location is not None
    parsed = urlparse(location)
    assert parsed.netloc == "accounts.google.com"
    query_params = parse_qs(parsed.query)
    assert "state" in query_params
    state = query_params["state"][0]

    # Verify state cookie is set
    settings = get_settings()
    cookie_val = response.cookies.get(settings.AUTH_OAUTH_STATE_COOKIE_NAME)
    assert cookie_val == state


def test_oauth_initiation_github(client: TestClient):
    """Verify GitHub OAuth initiation sets state cookie and returns redirect."""
    response = client.get("/v1/auth/github", follow_redirects=False)
    assert response.status_code == 307

    location = response.headers.get("location")
    assert location is not None
    parsed = urlparse(location)
    assert parsed.netloc == "github.com"
    query_params = parse_qs(parsed.query)
    assert "state" in query_params


def test_unsupported_oauth_provider(client: TestClient):
    """Verify requesting an unsupported provider returns 400 error."""
    response = client.get("/v1/auth/facebook")
    assert response.status_code == 400
    data = response.json()
    assert "Unsupported OAuth provider" in data["error"]["message"]


@pytest.mark.asyncio
async def test_oauth_callback_state_validation(client: TestClient):
    """Verify callback strictly validates state cookie and rejects missing or mismatched state."""
    settings = get_settings()

    # 1. Missing state parameter and missing cookie
    resp1 = client.get("/v1/auth/google/callback?code=fake_code")
    assert resp1.status_code == 400
    assert resp1.json()["error"]["details"]["code"] == "invalid_oauth_state"

    # 2. State mismatch
    client.cookies.set(settings.AUTH_OAUTH_STATE_COOKIE_NAME, "valid_state_123")
    resp2 = client.get("/v1/auth/google/callback?code=fake_code&state=wrong_state_456")
    assert resp2.status_code == 400
    assert resp2.json()["error"]["details"]["code"] == "invalid_oauth_state"

    # 3. Provider reported error
    resp3 = client.get("/v1/auth/google/callback?error=access_denied&state=valid_state_123")
    assert resp3.status_code == 400
    assert resp3.json()["error"]["details"]["code"] == "oauth_provider_error"


@patch("app.auth.providers.google.GoogleOAuthProvider.exchange_code", new_callable=AsyncMock)
@patch("app.auth.providers.google.GoogleOAuthProvider.get_identity", new_callable=AsyncMock)
def test_oauth_callback_successful_flow(mock_get_identity, mock_exchange_code, client: TestClient):
    """Verify complete successful OAuth callback issues session cookie and deletes state cookie."""
    settings = get_settings()

    mock_exchange_code.return_value = "mock_google_access_token"
    mock_get_identity.return_value = ProviderIdentity(
        provider="google",
        provider_subject="google_sub_999",
        email="oauth_login_user@example.com",
        display_name="Google User",
    )

    state = "secure_random_state_val"
    client.cookies.set(settings.AUTH_OAUTH_STATE_COOKIE_NAME, state)

    response = client.get(
        f"/v1/auth/google/callback?code=mock_auth_code&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{settings.WEB_BASE_URL}/account"

    # Session cookie must be present
    session_cookie = response.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
    assert session_cookie is not None
    assert session_cookie.startswith(settings.SESSION_TOKEN_PREFIX)

    # State cookie must be deleted
    state_cookie = response.cookies.get(settings.AUTH_OAUTH_STATE_COOKIE_NAME)
    assert state_cookie == "" or state_cookie is None


@patch("app.auth.providers.google.GoogleOAuthProvider.exchange_code", new_callable=AsyncMock)
def test_oauth_callback_exchange_failure(mock_exchange_code, client: TestClient):
    """Verify code exchange failure is safely handled without leaking credentials."""
    settings = get_settings()
    mock_exchange_code.side_effect = BehaviorSimAPIError(
        message="Failed to exchange authorization code with Google.",
        status_code=400,
        details={"code": "oauth_exchange_failed"},
    )

    state = "secure_state_val"
    client.cookies.set(settings.AUTH_OAUTH_STATE_COOKIE_NAME, state)

    response = client.get(
        f"/v1/auth/google/callback?code=invalid_auth_code&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 400
    data = response.json()
    assert data["error"]["details"]["code"] == "oauth_exchange_failed"

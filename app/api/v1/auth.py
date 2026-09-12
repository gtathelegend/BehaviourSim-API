"""OAuth authentication and session management endpoints."""

import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.providers.base import OAuthProvider
from app.auth.providers.github import GitHubOAuthProvider
from app.auth.providers.google import GoogleOAuthProvider
from app.auth.service import create_user_session, resolve_or_create_user_identity
from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError
from app.core.security import generate_oauth_state
from app.db.session import get_db

router = APIRouter(tags=["auth"])


def get_oauth_provider(provider_name: str) -> OAuthProvider:
    """Resolve the requested OAuth provider implementation."""
    name = provider_name.lower()
    if name == "google":
        return GoogleOAuthProvider()
    if name == "github":
        return GitHubOAuthProvider()
    raise BehaviorSimAPIError(
        message=f"Unsupported OAuth provider: '{provider_name}'. Supported providers: google, github.",
        status_code=400,
        details={"code": "unsupported_provider"},
    )


@router.get("/auth/{provider}")
async def oauth_login(provider: str) -> Response:
    """Initiate OAuth authorization redirect with CSRF state protection."""
    settings = get_settings()
    provider_client = get_oauth_provider(provider)

    state = generate_oauth_state()
    redirect_uri = f"{settings.OAUTH_REDIRECT_BASE_URL}/v1/auth/{provider}/callback"
    authorization_url = provider_client.get_authorization_url(redirect_uri=redirect_uri, state=state)

    response = RedirectResponse(url=authorization_url, status_code=307)
    response.set_cookie(
        key=settings.AUTH_OAUTH_STATE_COOKIE_NAME,
        value=state,
        max_age=settings.AUTH_OAUTH_STATE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return response


@router.get("/auth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> Response:
    """Handle OAuth provider callback, exchange code, resolve user, and issue session."""
    settings = get_settings()
    provider_client = get_oauth_provider(provider)

    if error:
        raise BehaviorSimAPIError(
            message=f"OAuth provider returned an error: {error}",
            status_code=400,
            details={"code": "oauth_provider_error"},
        )

    # 1. State / CSRF Validation
    cookie_state = request.cookies.get(settings.AUTH_OAUTH_STATE_COOKIE_NAME)
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        raise BehaviorSimAPIError(
            message="Missing or invalid OAuth state parameter.",
            status_code=400,
            details={"code": "invalid_oauth_state"},
        )

    if not code:
        raise BehaviorSimAPIError(
            message="Missing authorization code from OAuth provider.",
            status_code=400,
            details={"code": "oauth_exchange_failed"},
        )

    # 2. Token Exchange & Identity Retrieval
    redirect_uri = f"{settings.OAUTH_REDIRECT_BASE_URL}/v1/auth/{provider}/callback"
    access_token = await provider_client.exchange_code(code=code, redirect_uri=redirect_uri)
    identity = await provider_client.get_identity(access_token=access_token)

    # 3. Account Resolution / Creation
    user = resolve_or_create_user_identity(db=db, identity=identity)

    # 4. Create Application Session
    _, raw_session_token = create_user_session(db=db, user=user)

    # 5. Redirect to configured frontend dashboard (strictly controlled destination)
    destination_url = f"{settings.WEB_BASE_URL}/account"
    response = RedirectResponse(url=destination_url, status_code=303)

    # Delete single-use OAuth state cookie
    response.delete_cookie(key=settings.AUTH_OAUTH_STATE_COOKIE_NAME, path="/")

    # Set authenticated HttpOnly session cookie
    response.set_cookie(
        key=settings.AUTH_SESSION_COOKIE_NAME,
        value=raw_session_token,
        max_age=settings.AUTH_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=settings.is_production,
        path="/",
    )
    return response


@router.post("/auth/logout")
async def logout(
    response: Response,
    principal: AuthenticatedPrincipal = Depends(get_current_principal),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Invalidate current application session and clear session cookie."""
    settings = get_settings()

    if principal.authentication_method == "session" and principal.session:
        principal.session.revoked_at = datetime.now(timezone.utc)
        db.commit()

    # Clear session cookie on client
    response.delete_cookie(key=settings.AUTH_SESSION_COOKIE_NAME, path="/")

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "message": "Successfully logged out."},
    )

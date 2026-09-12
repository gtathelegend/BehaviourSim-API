"""Authentication principal and FastAPI dependency integration."""

from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.api_key import APIKey
from app.db.models.session import UserSession
from app.db.models.user import User
from app.db.session import get_db

bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Represents an authenticated caller (user, authentication method, and credentials)."""

    user: User
    authentication_method: str = "api_key"  # "api_key" | "session"
    api_key: Optional[APIKey] = None
    session: Optional[UserSession] = None

    @property
    def user_id(self):
        """Return the unique user identifier."""
        return self.user.id


async def get_current_principal(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AuthenticatedPrincipal:
    """Validate API key or application session token and resolve authenticated principal.

    Accepts credentials via:
    1. Authorization Bearer header (`bs_live_...` for API keys, `bs_sess_...` for session tokens)
    2. HttpOnly session cookie (`behaviorsim_session`)
    """
    settings = get_settings()

    # 1. Bearer Token in Authorization header
    if credentials and credentials.credentials:
        token = credentials.credentials

        # Case A: API Key
        if token.startswith(settings.API_KEY_PREFIX):
            from app.services.api_key import validate_api_key

            key_principal = validate_api_key(db, token)
            if key_principal is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or revoked API key.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if not key_principal.user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User account is deactivated.",
                )
            return AuthenticatedPrincipal(
                user=key_principal.user,
                authentication_method="api_key",
                api_key=key_principal.api_key,
            )

        # Case B: Session Token in Bearer Header
        if token.startswith(settings.SESSION_TOKEN_PREFIX):
            from app.auth.service import validate_session_token

            session = validate_session_token(db, token)
            if session is None or not session.user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired session token.",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return AuthenticatedPrincipal(
                user=session.user,
                authentication_method="session",
                session=session,
            )

        # Invalid token prefix
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unrecognized token format.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Session Cookie
    cookie_token = request.cookies.get(settings.AUTH_SESSION_COOKIE_NAME)
    if cookie_token:
        from app.auth.service import validate_session_token

        session = validate_session_token(db, cookie_token)
        if session is not None and session.user.is_active:
            return AuthenticatedPrincipal(
                user=session.user,
                authentication_method="session",
                session=session,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session cookie.",
        )

    # Neither Bearer token nor session cookie provided
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing authentication credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )

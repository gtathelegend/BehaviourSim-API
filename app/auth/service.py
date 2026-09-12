"""Authentication service orchestrating identity resolution, account creation, and session lifecycle."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.providers.base import ProviderIdentity
from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError
from app.core.security import generate_session_token, hash_session_token
from app.db.models.auth_identity import AuthIdentity
from app.db.models.session import UserSession
from app.db.models.user import User

logger = logging.getLogger("behaviorsim_api.auth.service")


def resolve_or_create_user_identity(db: Session, identity: ProviderIdentity) -> User:
    """Resolve an existing authenticated user or create a new account.

    Anti-Account Takeover Policy:
    1. If (provider, provider_subject) matches an existing AuthIdentity, return the associated User.
    2. If no AuthIdentity matches and the email is not registered, create a new User and AuthIdentity.
    3. If the email is already registered under a different account without this provider identity,
       reject automatic merging with a 409 Conflict error to prevent account takeover attacks.
    """
    # 1. Existing identity lookup
    stmt = select(AuthIdentity).where(
        AuthIdentity.provider == identity.provider,
        AuthIdentity.provider_subject == identity.provider_subject,
    )
    existing_identity = db.scalars(stmt).first()

    if existing_identity:
        user = existing_identity.user
        if not user.is_active:
            logger.warning("Authentication rejected: user_id=%s is inactive", user.id)
            raise BehaviorSimAPIError(
                message="Your account is deactivated.",
                status_code=403,
                details={"code": "account_inactive"},
            )
        logger.info("Resolved existing user_id=%s via provider=%s", user.id, identity.provider)
        return user

    # 2. Check for email collision with existing account
    email_stmt = select(User).where(User.email == identity.email)
    existing_user_by_email = db.scalars(email_stmt).first()

    if existing_user_by_email:
        logger.warning(
            "Account takeover prevention: OAuth login with provider=%s subject=%s attempted for already registered email=%s",
            identity.provider,
            identity.provider_subject,
            identity.email,
        )
        raise BehaviorSimAPIError(
            message="An account with this email address already exists. Please sign in with your original provider to link identities.",
            status_code=409,
            details={"code": "account_linking_required"},
        )

    # 3. Create new User and AuthIdentity atomically
    new_user = User(
        id=uuid.uuid4(),
        email=identity.email,
        display_name=identity.display_name,
        is_active=True,
    )
    db.add(new_user)
    db.flush()

    new_auth_identity = AuthIdentity(
        id=uuid.uuid4(),
        user_id=new_user.id,
        provider=identity.provider,
        provider_subject=identity.provider_subject,
    )
    db.add(new_auth_identity)
    db.commit()
    db.refresh(new_user)

    logger.info("Created new user_id=%s via provider=%s", new_user.id, identity.provider)
    return new_user


def create_user_session(db: Session, user: User) -> Tuple[UserSession, str]:
    """Create a new application session for an authenticated user.

    Returns:
        Tuple of (UserSession database entity, raw session token string)
    """
    settings = get_settings()
    raw_token, token_hash = generate_session_token(prefix=settings.SESSION_TOKEN_PREFIX)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.AUTH_SESSION_MAX_AGE_SECONDS)

    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    logger.info("Created session_id=%s for user_id=%s", session.id, user.id)
    return session, raw_token


def validate_session_token(db: Session, raw_token: str) -> Optional[UserSession]:
    """Validate a presented raw session token against the database."""
    settings = get_settings()
    if not raw_token or not raw_token.startswith(settings.SESSION_TOKEN_PREFIX):
        return None

    token_hash = hash_session_token(raw_token)
    stmt = select(UserSession).where(
        UserSession.token_hash == token_hash,
        UserSession.revoked_at.is_(None),
    )
    session = db.scalars(stmt).first()
    if not session:
        return None

    # Check expiration
    now = datetime.now(timezone.utc)
    # Account for SQLite naive datetime in test environments
    expires_at = session.expires_at if session.expires_at.tzinfo else session.expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        logger.info("Session_id=%s has expired", session.id)
        return None

    return session


def revoke_user_session(db: Session, raw_token: str) -> bool:
    """Soft-revoke an active user session by marking it revoked."""
    session = validate_session_token(db, raw_token)
    if not session:
        return False

    session.revoked_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("Revoked session_id=%s for user_id=%s", session.id, session.user_id)
    return True

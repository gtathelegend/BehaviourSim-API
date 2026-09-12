"""API key lifecycle service managing creation, listing, revocation, and validation."""

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal
from app.core.config import get_settings
from app.core.security import generate_api_key, verify_api_key
from app.db.models.api_key import APIKey
from app.db.models.user import User

logger = logging.getLogger("behaviorsim_api.services.api_key")


class APIKeyCreateResult(BaseModel):
    """Result returned upon creating an API key, including the raw secret exactly once."""

    id: uuid.UUID
    name: str
    key: str  # Raw secret, exposed only on initial creation
    key_prefix: str
    created_at: datetime


def create_api_key(db: Session, user: User, name: str) -> APIKeyCreateResult:
    """Create a new API key for the specified user.

    The raw key string is returned exactly once in the result object and is never persisted.
    """
    settings = get_settings()
    raw_key, key_prefix, key_hash = generate_api_key(prefix=settings.API_KEY_PREFIX)

    api_key = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        name=name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        is_active=True,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    logger.info("Created API key id=%s prefix=%s for user_id=%s", api_key.id, key_prefix, user.id)

    return APIKeyCreateResult(
        id=api_key.id,
        name=api_key.name,
        key=raw_key,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
    )


def list_api_keys(db: Session, user: User) -> List[APIKey]:
    """List API keys belonging to a user, returning metadata only."""
    stmt = (
        select(APIKey)
        .where(APIKey.user_id == user.id)
        .order_by(APIKey.created_at.desc())
    )
    return list(db.scalars(stmt).all())


def revoke_api_key(db: Session, user: User, key_id: uuid.UUID) -> Optional[APIKey]:
    """Soft-revoke an existing API key.

    Marks the key as inactive and records revocation timestamp without deleting row.
    Returns None if key is not found or does not belong to user.
    """
    stmt = select(APIKey).where(APIKey.id == key_id, APIKey.user_id == user.id)
    api_key = db.scalars(stmt).first()
    if not api_key:
        logger.warning("Revocation attempted for non-existent or unowned key_id=%s by user_id=%s", key_id, user.id)
        return None

    if api_key.is_active:
        api_key.is_active = False
        api_key.revoked_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(api_key)
        logger.info("Revoked API key id=%s prefix=%s for user_id=%s", api_key.id, api_key.key_prefix, user.id)

    return api_key


def validate_api_key(db: Session, raw_key: str) -> Optional[AuthenticatedPrincipal]:
    """Validate a presented raw API key and resolve the authenticated principal.

    Workflow:
    1. Extract the prefix from the presented key.
    2. Query active, non-revoked candidate keys matching the prefix.
    3. Verify cryptographic hash using constant-time comparison.
    4. Confirm user exists and is active.
    5. Update `last_used_at` timestamp.
    6. Return AuthenticatedPrincipal.
    """
    settings = get_settings()
    if not raw_key or not raw_key.startswith(settings.API_KEY_PREFIX):
        return None

    key_prefix = raw_key[:16]
    stmt = (
        select(APIKey)
        .where(
            APIKey.key_prefix == key_prefix,
            APIKey.is_active.is_(True),
            APIKey.revoked_at.is_(None),
        )
    )
    candidates = list(db.scalars(stmt).all())

    matched_key: Optional[APIKey] = None
    for candidate in candidates:
        if verify_api_key(raw_key, candidate.key_hash):
            matched_key = candidate
            break

    if matched_key is None:
        return None

    # Resolve user
    user = db.get(User, matched_key.user_id)
    if user is None or not user.is_active:
        logger.warning("API key matched prefix=%s but owner is missing or inactive", key_prefix)
        return None

    # Update last_used_at timestamp
    matched_key.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return AuthenticatedPrincipal(user=user, api_key=matched_key)

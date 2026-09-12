"""Tests for database models, constraints, and relationships."""

import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.api_key import APIKey
from app.db.models.auth_identity import AuthIdentity
from app.db.models.user import User


def test_create_user(db_session):
    """Verify user can be created with expected default fields."""
    user = User(
        email="test@example.com",
        display_name="Test User",
    )
    db_session.add(user)
    db_session.commit()

    assert user.id is not None
    assert isinstance(user.id, uuid.UUID)
    assert user.email == "test@example.com"
    assert user.display_name == "Test User"
    assert user.is_active is True
    assert user.created_at is not None
    assert user.updated_at is not None


def test_user_unique_email(db_session):
    """Verify duplicate email violates uniqueness constraint."""
    user1 = User(email="duplicate@example.com", display_name="User One")
    db_session.add(user1)
    db_session.commit()

    user2 = User(email="duplicate@example.com", display_name="User Two")
    db_session.add(user2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_auth_identity_creation_and_relationships(db_session):
    """Verify auth identity links to user and supports multiple providers."""
    user = User(email="oauth_user@example.com", display_name="OAuth User")
    db_session.add(user)
    db_session.commit()

    identity_google = AuthIdentity(
        user_id=user.id,
        provider="google",
        provider_subject="google-sub-12345",
    )
    identity_github = AuthIdentity(
        user_id=user.id,
        provider="github",
        provider_subject="github-sub-67890",
    )
    db_session.add_all([identity_google, identity_github])
    db_session.commit()

    db_session.refresh(user)
    assert len(user.identities) == 2
    providers = {ident.provider for ident in user.identities}
    assert providers == {"google", "github"}


def test_auth_identity_provider_subject_uniqueness(db_session):
    """Verify compound uniqueness on (provider, provider_subject)."""
    user1 = User(email="user1@example.com")
    user2 = User(email="user2@example.com")
    db_session.add_all([user1, user2])
    db_session.commit()

    ident1 = AuthIdentity(
        user_id=user1.id,
        provider="google",
        provider_subject="shared-sub-123",
    )
    db_session.add(ident1)
    db_session.commit()

    ident2 = AuthIdentity(
        user_id=user2.id,
        provider="google",
        provider_subject="shared-sub-123",
    )
    db_session.add(ident2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_api_key_creation_and_ownership(db_session):
    """Verify API key creation, relationship to user, and revocation fields."""
    user = User(email="apikey_owner@example.com")
    db_session.add(user)
    db_session.commit()

    key = APIKey(
        user_id=user.id,
        name="Production Key",
        key_prefix="bs_live_12345678",
        key_hash="fake_hash_string_64_characters_long_0123456789abcdef0123456789abcdef",
    )
    db_session.add(key)
    db_session.commit()

    assert key.id is not None
    assert key.is_active is True
    assert key.last_used_at is None
    assert key.revoked_at is None
    assert key.user.email == "apikey_owner@example.com"

    # Revocation simulation
    now = datetime.now(timezone.utc)
    key.is_active = False
    key.revoked_at = now
    db_session.commit()

    db_session.refresh(key)
    assert key.is_active is False
    assert key.revoked_at is not None
    # Account for SQLite stripping tzinfo on storage in unit tests
    revoked_tz = key.revoked_at if key.revoked_at.tzinfo else key.revoked_at.replace(tzinfo=timezone.utc)
    assert revoked_tz == now

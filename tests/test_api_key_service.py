"""Tests for API key lifecycle service."""

import pytest
from app.db.models.user import User
from app.services.api_key import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
    validate_api_key,
)


@pytest.fixture
def test_user(db_session):
    """Fixture providing an active user in the database."""
    user = User(
        email="developer@example.com",
        display_name="Dev User",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_create_api_key_returns_raw_key_once(db_session, test_user):
    """Verify key creation provides raw secret, correct prefix, and persists hash."""
    result = create_api_key(db_session, test_user, name="CI/CD Key")

    assert result.id is not None
    assert result.name == "CI/CD Key"
    assert result.key.startswith("bs_live_")
    assert result.key_prefix == result.key[:16]
    assert result.created_at is not None

    # Verify that listing keys returns metadata without raw key
    keys = list_api_keys(db_session, test_user)
    assert len(keys) == 1
    key_entry = keys[0]
    assert key_entry.id == result.id
    assert key_entry.key_prefix == result.key_prefix
    assert not hasattr(key_entry, "key")  # Raw key is never an attribute on APIKey model
    assert key_entry.key_hash != result.key  # Stored hash is not the raw key


def test_list_api_keys_scoped_to_user(db_session, test_user):
    """Verify list_api_keys only returns keys owned by the querying user."""
    other_user = User(email="other@example.com", is_active=True)
    db_session.add(other_user)
    db_session.commit()

    create_api_key(db_session, test_user, name="User1 Key")
    create_api_key(db_session, other_user, name="User2 Key")

    user1_keys = list_api_keys(db_session, test_user)
    assert len(user1_keys) == 1
    assert user1_keys[0].name == "User1 Key"


def test_revoke_api_key(db_session, test_user):
    """Verify key revocation marks key inactive and idempotent."""
    created = create_api_key(db_session, test_user, name="Key to Revoke")

    # Revoke key
    revoked = revoke_api_key(db_session, test_user, created.id)
    assert revoked is not None
    assert revoked.is_active is False
    assert revoked.revoked_at is not None

    # Idempotent second revocation
    revoked_again = revoke_api_key(db_session, test_user, created.id)
    assert revoked_again is not None
    assert revoked_again.is_active is False


def test_revoke_unowned_key_returns_none(db_session, test_user):
    """Verify a user cannot revoke another user's API key."""
    other_user = User(email="other2@example.com", is_active=True)
    db_session.add(other_user)
    db_session.commit()

    created = create_api_key(db_session, other_user, name="Other Key")

    # Attempt revocation as test_user
    result = revoke_api_key(db_session, test_user, created.id)
    assert result is None


def test_validate_api_key_lifecycle(db_session, test_user):
    """Verify valid key authenticates, updates last_used_at, and revoked key is rejected."""
    created = create_api_key(db_session, test_user, name="Auth Key")

    # 1. Valid authentication
    principal = validate_api_key(db_session, created.key)
    assert principal is not None
    assert principal.user.id == test_user.id
    assert principal.api_key.id == created.id
    assert principal.api_key.last_used_at is not None

    # 2. Invalid raw key
    assert validate_api_key(db_session, created.key + "tamper") is None
    assert validate_api_key(db_session, "bs_live_completely_fake") is None
    assert validate_api_key(db_session, "") is None

    # 3. Revoked key authentication fails
    revoke_api_key(db_session, test_user, created.id)
    assert validate_api_key(db_session, created.key) is None


def test_validate_api_key_inactive_user(db_session, test_user):
    """Verify API key fails authentication if the owner user account is deactivated."""
    created = create_api_key(db_session, test_user, name="Active Key Inactive User")

    # Deactivate user
    test_user.is_active = False
    db_session.commit()

    assert validate_api_key(db_session, created.key) is None

"""Tests for identity resolution, account creation, and anti-takeover linking policy."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.providers.base import ProviderIdentity
from app.auth.service import resolve_or_create_user_identity
from app.core.errors import BehaviorSimAPIError
from app.db.models.auth_identity import AuthIdentity
from app.db.models.user import User


def test_create_new_user_from_identity(db_session: Session):
    """Verify new identity creates User and AuthIdentity in a single transaction."""
    identity = ProviderIdentity(
        provider="google",
        provider_subject="google_sub_1001",
        email="newuser@example.com",
        display_name="New User",
    )

    user = resolve_or_create_user_identity(db_session, identity)
    assert user.id is not None
    assert user.email == "newuser@example.com"
    assert user.display_name == "New User"
    assert user.is_active is True

    # Verify associated AuthIdentity
    ident = db_session.scalars(
        select(AuthIdentity).where(AuthIdentity.user_id == user.id)
    ).first()
    assert ident is not None
    assert ident.provider == "google"
    assert ident.provider_subject == "google_sub_1001"


def test_resolve_existing_identity(db_session: Session):
    """Verify existing provider and subject resolves the previously created user."""
    identity = ProviderIdentity(
        provider="github",
        provider_subject="github_sub_2002",
        email="existing@example.com",
        display_name="Existing Dev",
    )

    user1 = resolve_or_create_user_identity(db_session, identity)
    user2 = resolve_or_create_user_identity(db_session, identity)

    assert user1.id == user2.id


def test_email_collision_rejects_automatic_takeover(db_session: Session):
    """Verify anti-account-takeover policy: email collision with unlinked account raises 409."""
    # Existing user registered originally
    existing_user = User(
        email="target_victim@example.com",
        display_name="Original Account",
        is_active=True,
    )
    db_session.add(existing_user)
    db_session.commit()

    # Attacker or secondary provider presenting same email
    colliding_identity = ProviderIdentity(
        provider="github",
        provider_subject="github_attacker_999",
        email="target_victim@example.com",
        display_name="Attacker Identity",
    )

    with pytest.raises(BehaviorSimAPIError) as exc_info:
        resolve_or_create_user_identity(db_session, colliding_identity)

    assert exc_info.value.status_code == 409
    assert exc_info.value.details.get("code") == "account_linking_required"


def test_inactive_user_login_rejected(db_session: Session):
    """Verify deactivated user cannot authenticate through OAuth identity."""
    identity = ProviderIdentity(
        provider="google",
        provider_subject="google_deactivated_sub",
        email="deactivated@example.com",
        display_name="Deactivated User",
    )

    user = resolve_or_create_user_identity(db_session, identity)
    user.is_active = False
    db_session.commit()

    with pytest.raises(BehaviorSimAPIError) as exc_info:
        resolve_or_create_user_identity(db_session, identity)

    assert exc_info.value.status_code == 403
    assert exc_info.value.details.get("code") == "account_inactive"

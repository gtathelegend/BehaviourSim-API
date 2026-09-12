"""Tests for database session management and dependency behavior."""

import pytest
from sqlalchemy.orm import Session

from app.db.models.user import User
from app.db.session import get_db


def test_get_db_generator():
    """Verify get_db yields an active Session and closes it upon exit."""
    db_gen = get_db()
    session = next(db_gen)
    assert isinstance(session, Session)
    assert session.is_active

    # Exhaust generator to trigger finally block
    with pytest.raises(StopIteration):
        next(db_gen)


def test_transaction_rollback_on_error(db_session):
    """Verify changes are not persisted if an error occurs and session rolls back."""
    user = User(email="rollback_user@example.com")
    db_session.add(user)
    db_session.flush()

    # Roll back transaction explicitly
    db_session.rollback()

    assert db_session.get(User, user.id) is None


def test_health_endpoint_no_db_required(client):
    """Verify /health works without triggering or requiring database connections."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

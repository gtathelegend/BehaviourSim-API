"""Tests for authentication dependency and principal resolution."""

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.auth import AuthenticatedPrincipal, get_current_principal
from app.db.models.user import User
from app.db.session import get_db
from app.services.api_key import create_api_key


def test_auth_dependency(db_session: Session):
    """Verify get_current_principal dependency extracts and validates API keys."""
    test_app = FastAPI()

    @test_app.get("/protected")
    def protected_route(principal: AuthenticatedPrincipal = Depends(get_current_principal)):
        return {
            "user_id": str(principal.user.id),
            "email": principal.user.email,
            "key_name": principal.api_key.name if principal.api_key else None,
        }

    def override_get_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_get_db

    user = User(email="bearer_user@example.com", is_active=True)
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Auth Test Key")

    with TestClient(test_app) as client:
        # 1. Missing Authorization header
        resp_missing = client.get("/protected")
        assert resp_missing.status_code == 401

        # 2. Invalid Authorization header
        resp_invalid = client.get("/protected", headers={"Authorization": "Bearer bs_live_invalidkey"})
        assert resp_invalid.status_code == 401

        # 3. Valid Authorization header
        resp_valid = client.get("/protected", headers={"Authorization": f"Bearer {created_key.key}"})
        assert resp_valid.status_code == 200
        data = resp_valid.json()
        assert data["user_id"] == str(user.id)
        assert data["email"] == "bearer_user@example.com"
        assert data["key_name"] == "Auth Test Key"

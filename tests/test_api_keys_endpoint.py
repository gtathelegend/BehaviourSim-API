"""Tests for /v1/api-keys HTTP endpoints."""

import uuid
import pytest
from fastapi.testclient import TestClient

from app.auth.service import create_user_session
from app.db.models.user import User
from app.services.plan import get_or_create_free_plan


@pytest.fixture
def api_test_user(db_session):
    """Create a user with seeded free plan for API key endpoint testing."""
    plan = get_or_create_free_plan(db_session)
    user = User(
        email="key_tester@example.com",
        display_name="Key Tester",
        is_active=True,
        plan_id=plan.id,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def auth_headers(db_session, api_test_user):
    """Provide authorization bearer headers for session user."""
    _, session_token = create_user_session(db_session, api_test_user)
    return {"Authorization": f"Bearer {session_token}"}


def test_api_keys_unauthenticated_rejected(client: TestClient):
    """Verify unauthenticated requests are rejected with 401."""
    assert client.get("/v1/api-keys").status_code == 401
    assert client.post("/v1/api-keys", json={"name": "Test"}).status_code == 401
    assert client.delete(f"/v1/api-keys/{uuid.uuid4()}").status_code == 401


def test_api_keys_lifecycle_crud(client: TestClient, auth_headers):
    """Verify complete CRUD lifecycle through HTTP endpoints."""
    # 1. Initial list is empty
    resp = client.get("/v1/api-keys", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []

    # 2. Create an API key
    create_resp = client.post(
        "/v1/api-keys",
        headers=auth_headers,
        json={"name": "CLI Token"},
    )
    assert create_resp.status_code == 201
    data = create_resp.json()
    assert data["name"] == "CLI Token"
    assert "key" in data
    assert data["key"].startswith("bs_live_")
    key_id = data["id"]

    # 3. List keys returns metadata without raw key
    list_resp = client.get("/v1/api-keys", headers=auth_headers)
    assert list_resp.status_code == 200
    items = list_resp.json()
    assert len(items) == 1
    assert items[0]["id"] == key_id
    assert items[0]["name"] == "CLI Token"
    assert "key" not in items[0]
    assert items[0]["is_active"] is True

    # 4. Revoke key
    del_resp = client.delete(f"/v1/api-keys/{key_id}", headers=auth_headers)
    assert del_resp.status_code == 200
    assert del_resp.json() == {"status": "revoked", "id": key_id}

    # 5. Revoking non-existent or already revoked key
    del_again = client.delete(f"/v1/api-keys/{uuid.uuid4()}", headers=auth_headers)
    assert del_again.status_code == 404

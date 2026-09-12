"""Deterministic local smoke test verifying end-to-end API lifecycle and startup/shutdown."""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.rate_limit import default_rate_limiter
from app.db.models.user import User
from app.main import app
from app.services.api_key import create_api_key, revoke_api_key


def test_application_startup_and_shutdown():
    """Verify application starts, runs lifespan checks, and shuts down cleanly."""
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_production_smoke_flow(client: TestClient, db_session: Session):
    """Execute end-to-end verification across user, key, account, presets, simulation, and revocation."""
    default_rate_limiter.reset()

    # 1. Create user
    user = User(email="smoke_test_user@example.com")
    db_session.add(user)
    db_session.commit()

    # 2. Create API key
    created_key = create_api_key(db_session, user, name="Smoke Test Key")
    headers = {"Authorization": f"Bearer {created_key.key}"}

    # 3. GET /v1/account
    account_resp = client.get("/v1/account", headers=headers)
    assert account_resp.status_code == 200
    account_data = account_resp.json()
    assert account_data["email"] == "smoke_test_user@example.com"
    assert account_data["plan"] == "free"

    # 4. GET /v1/usage (Initial state: 0 consumed)
    usage_init = client.get("/v1/usage", headers=headers)
    assert usage_init.status_code == 200
    assert usage_init.json()["usage"]["requests"] == 0
    assert usage_init.json()["usage"]["interactions"] == 0

    # 5. GET /v1/presets
    presets_resp = client.get("/v1/presets")
    assert presets_resp.status_code == 200
    preset_names = [p["name"] for p in presets_resp.json()]
    assert "education" in preset_names

    # 6. POST /v1/simulations
    sim_resp = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 25, "seed": 42},
    )
    assert sim_resp.status_code == 200
    sim_data = sim_resp.json()
    assert sim_data["preset"] == "education"
    assert sim_data["num_interactions"] == 25
    assert sim_data["seed"] == 42
    assert len(sim_data["data"]) == 25
    assert sim_data["metadata"]["reproducible"] is True
    assert sim_data["metadata"]["behaviorsim_version"] == "1.0.1"

    # 7. Verify usage updated (1 request, 25 interactions)
    usage_after = client.get("/v1/usage", headers=headers)
    assert usage_after.status_code == 200
    assert usage_after.json()["usage"]["requests"] == 1
    assert usage_after.json()["usage"]["interactions"] == 25
    assert usage_after.json()["remaining"]["requests"] == 99
    assert usage_after.json()["remaining"]["interactions"] == 9975

    # 8. Revoke API key
    revoke_api_key(db_session, user, created_key.id)

    # 9. Verify authentication fails on revoked key
    auth_fail = client.get("/v1/account", headers=headers)
    assert auth_fail.status_code == 401

    default_rate_limiter.reset()

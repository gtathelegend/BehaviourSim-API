"""Comprehensive tests for BehaviorSim API Simulation and Preset endpoints."""

from unittest.mock import patch
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.rate_limit import default_rate_limiter
from app.core.security import generate_session_token, hash_session_token
from app.db.models.plan import Plan
from app.db.models.session import UserSession
from app.db.models.usage import MonthlyUsage, UsageEvent
from app.db.models.user import User
from app.services.api_key import create_api_key, revoke_api_key
from app.services.usage import get_current_period_start


@pytest.fixture(autouse=True)
def reset_limiter():
    """Reset rate limiter before every test in this module."""
    default_rate_limiter.reset()
    yield
    default_rate_limiter.reset()


# ============================================================================
# 1. Preset Endpoints Tests (GET /v1/presets, GET /v1/presets/{preset})
# ============================================================================

def test_list_presets(client: TestClient):
    """Verify public preset catalog returns all 4 published domain presets."""
    resp = client.get("/v1/presets")
    assert resp.status_code == 200
    presets = resp.json()
    assert isinstance(presets, list)
    names = [p["name"] for p in presets]
    for expected in ["education", "finance", "healthcare", "mobile_app"]:
        assert expected in names

    education_preset = next(p for p in presets if p["name"] == "education")
    assert education_preset["available"] is True
    assert "average" in education_preset["supported_profiles"]
    assert "Optimal" in education_preset["supported_states"]


def test_get_preset_detail(client: TestClient):
    """Verify single preset inspection endpoint."""
    resp = client.get("/v1/presets/education")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "education"
    assert data["default_profile"] == "average"
    assert "fast_accurate" in data["supported_profiles"]
    assert "Underload" in data["supported_states"]


def test_get_preset_alias_resolution(client: TestClient):
    """Verify 'mobile' alias seamlessly resolves to 'mobile_app'."""
    resp = client.get("/v1/presets/mobile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "mobile_app"
    assert "casual_browser" in data["supported_profiles"]


def test_get_unknown_preset_returns_404(client: TestClient):
    """Verify unknown preset name returns HTTP 404 with structured error."""
    resp = client.get("/v1/presets/quantum_physics")
    assert resp.status_code == 404
    err = resp.json()
    assert err["error"]["details"]["code"] == "preset_not_found"


# ============================================================================
# 2. Authentication Tests (POST /v1/simulations)
# ============================================================================

def test_simulation_requires_authentication(client: TestClient):
    """Reject unauthenticated simulation requests with HTTP 401."""
    resp = client.post(
        "/v1/simulations",
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp.status_code == 401


def test_simulation_rejects_invalid_api_key(client: TestClient):
    """Reject invalid API key with HTTP 401."""
    resp = client.post(
        "/v1/simulations",
        headers={"Authorization": "Bearer bs_live_invalidkey123"},
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp.status_code == 401


def test_simulation_rejects_revoked_api_key(client: TestClient, db_session: Session):
    """Reject revoked API key with HTTP 401."""
    user = User(email="revoked_sim_user@example.com")
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Revoked Key")
    revoke_api_key(db_session, user, created_key.id)

    resp = client.post(
        "/v1/simulations",
        headers={"Authorization": f"Bearer {created_key.key}"},
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp.status_code == 401


def test_simulation_supports_session_cookie(client: TestClient, db_session: Session):
    """Verify simulation endpoint works via authenticated user session cookie."""
    from app.auth.service import create_user_session

    user = User(email="session_sim_user@example.com")
    db_session.add(user)
    db_session.commit()

    _, raw_token = create_user_session(db_session, user)

    client.cookies.set("behaviorsim_session", raw_token)
    resp = client.post(
        "/v1/simulations",
        json={"preset": "education", "num_interactions": 10, "seed": 42},
    )
    assert resp.status_code == 200
    client.cookies.clear()


def test_simulation_rejects_inactive_user(client: TestClient, db_session: Session):
    """Reject inactive user account with HTTP 401."""
    user = User(email="inactive_sim_user@example.com", is_active=False)
    db_session.add(user)
    db_session.commit()

    created_key = create_api_key(db_session, user, name="Inactive Key")
    resp = client.post(
        "/v1/simulations",
        headers={"Authorization": f"Bearer {created_key.key}"},
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp.status_code == 401


# ============================================================================
# 3. Request Validation Tests
# ============================================================================

def test_simulation_validation_errors(client: TestClient, db_session: Session):
    """Verify validation boundaries for simulation request payloads."""
    user = User(email="validation_sim_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Val Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Zero interactions -> 422
    default_rate_limiter.reset()
    resp = client.post("/v1/simulations", headers=headers, json={"preset": "education", "num_interactions": 0})
    assert resp.status_code == 422

    # Negative interactions -> 422
    default_rate_limiter.reset()
    resp = client.post("/v1/simulations", headers=headers, json={"preset": "education", "num_interactions": -10})
    assert resp.status_code == 422

    # Float interactions -> 422
    default_rate_limiter.reset()
    resp = client.post("/v1/simulations", headers=headers, json={"preset": "education", "num_interactions": 10.5})
    assert resp.status_code == 422

    # String interactions -> 422
    default_rate_limiter.reset()
    resp = client.post("/v1/simulations", headers=headers, json={"preset": "education", "num_interactions": "ten"})
    assert resp.status_code == 422

    # Exceeding plan max interactions (free plan max = 1000) -> 400 interaction_limit_exceeded
    default_rate_limiter.reset()
    resp = client.post("/v1/simulations", headers=headers, json={"preset": "education", "num_interactions": 1500})
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["code"] == "interaction_limit_exceeded"

    # Unknown preset -> 400 unsupported_preset
    default_rate_limiter.reset()
    resp = client.post("/v1/simulations", headers=headers, json={"preset": "nonexistent", "num_interactions": 10})
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["code"] == "unsupported_preset"

    # Invalid profile for education preset -> 400 invalid_profile
    default_rate_limiter.reset()
    resp = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 10, "profile": "unknown_profile"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["code"] == "invalid_profile"

    # Invalid initial state for education preset -> 400 invalid_initial_state
    default_rate_limiter.reset()
    resp = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 10, "initial_state": "FakeState"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["details"]["code"] == "invalid_initial_state"


# ============================================================================
# 4. Simulation Execution & Domain Presets Tests
# ============================================================================

@pytest.mark.parametrize("preset_name", ["education", "finance", "healthcare", "mobile", "mobile_app"])
def test_simulation_all_presets_execution(preset_name: str, client: TestClient, db_session: Session):
    """Verify execution of all published domain presets including alias."""
    user = User(email=f"sim_{preset_name}@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    resp = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": preset_name, "num_interactions": 15, "seed": 100},
    )
    assert resp.status_code == 200
    payload = resp.json()

    assert uuid.UUID(payload["simulation_id"])  # Valid UUID format
    expected_canonical = "mobile_app" if preset_name in ["mobile", "mobile_app"] else preset_name
    assert payload["preset"] == expected_canonical
    assert payload["num_interactions"] == 15
    assert payload["seed"] == 100
    assert len(payload["data"]) == 15
    assert payload["metadata"]["behaviorsim_version"] == "1.0.1"
    assert payload["metadata"]["reproducible"] is True
    assert payload["metadata"]["compute_ms"] > 0


def test_simulation_seed_reproducibility(client: TestClient, db_session: Session):
    """Verify identical seeds produce identical simulation datasets."""
    user = User(email="reproducibility_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Run 1 with seed=42
    resp1 = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 20, "seed": 42},
    )
    assert resp1.status_code == 200

    # Run 2 with seed=42
    resp2 = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 20, "seed": 42},
    )
    assert resp2.status_code == 200

    assert resp1.json()["data"] == resp2.json()["data"]
    # Simulation IDs must still be unique per run
    assert resp1.json()["simulation_id"] != resp2.json()["simulation_id"]


def test_simulation_unseeded(client: TestClient, db_session: Session):
    """Verify unseeded simulations return seed=null and reproducible=false."""
    user = User(email="unseeded_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    resp = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["seed"] is None
    assert payload["metadata"]["reproducible"] is False


# ============================================================================
# 5. Quota Accounting & Rate Limit Integration Tests
# ============================================================================

def test_simulation_deducts_monthly_quota(client: TestClient, db_session: Session):
    """Verify simulation execution increments monthly requests and interactions."""
    user = User(email="quota_deduct_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # 1. Run simulation of 50 interactions
    resp = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 50, "seed": 42},
    )
    assert resp.status_code == 200

    # 2. Check /v1/usage
    usage_resp = client.get("/v1/usage", headers=headers)
    assert usage_resp.status_code == 200
    usage_data = usage_resp.json()
    assert usage_data["usage"]["requests"] == 1
    assert usage_data["usage"]["interactions"] == 50
    assert usage_data["remaining"]["requests"] == 99
    assert usage_data["remaining"]["interactions"] == 9950


def test_simulation_rejected_when_quota_exhausted(client: TestClient, db_session: Session):
    """Verify simulation returns HTTP 429 when monthly quota is exhausted and does not execute."""
    # Create custom plan with limit of 1 request
    tight_plan = Plan(
        name="tight_plan",
        monthly_requests=1,
        monthly_interactions=1000,
        max_interactions_per_request=1000,
        requests_per_minute=100,
        max_concurrent_simulations=1,
        max_api_keys=5,
    )
    db_session.add(tight_plan)
    db_session.commit()

    user = User(email="exhausted_quota_user@example.com", plan_id=tight_plan.id)
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # 1st simulation consumes the only allowed request
    resp1 = client.post(
        "/v1/simulations",
        headers=headers,
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp1.status_code == 200

    # 2nd simulation must be rejected with 429 quota_exceeded
    with patch("app.services.simulation.Simulator.from_preset") as mock_sim:
        resp2 = client.post(
            "/v1/simulations",
            headers=headers,
            json={"preset": "education", "num_interactions": 10},
        )
        assert resp2.status_code == 429
        assert resp2.json()["error"]["details"]["code"] == "quota_exceeded"
        # Verify BehaviorSim was NOT called
        mock_sim.assert_not_called()


# ============================================================================
# 6. Fault-Injection & Quota Refund Tests
# ============================================================================

def test_simulation_failure_refunds_quota(client: TestClient, db_session: Session):
    """Verify internal execution failure refunds reserved quota and logs failure event."""
    user = User(email="failure_refund_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Mock execute_simulation to simulate an unexpected crash
    with patch(
        "app.api.v1.simulations.execute_simulation",
        side_effect=RuntimeError("Simulated internal engine crash"),
    ):
        resp = client.post(
            "/v1/simulations",
            headers=headers,
            json={"preset": "education", "num_interactions": 50},
        )
        assert resp.status_code == 500
        assert resp.json()["error"]["details"]["code"] == "simulation_generation_failed"

    # Quota must be refunded: request_count=0, interaction_count=0
    period_start = get_current_period_start()
    usage = db_session.query(MonthlyUsage).filter_by(user_id=user.id, period_start=period_start).first()
    assert usage is not None
    assert usage.request_count == 0
    assert usage.interaction_count == 0

    # Failure audit event must be logged
    event = db_session.query(UsageEvent).filter_by(user_id=user.id, event_type="simulation_failed").first()
    assert event is not None
    assert event.success is False

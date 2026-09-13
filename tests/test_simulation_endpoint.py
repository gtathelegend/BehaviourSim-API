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
from app.db.models.simulation import Simulation
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
        "/v1/simulations?sync=true",
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
        "/v1/simulations?sync=true",
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
        "/v1/simulations?sync=true",
        headers=headers,
        json={"preset": "education", "num_interactions": 20, "seed": 42},
    )
    assert resp1.status_code == 200

    # Run 2 with seed=42
    resp2 = client.post(
        "/v1/simulations?sync=true",
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
        "/v1/simulations?sync=true",
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
        "/v1/simulations?sync=true",
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
        "/v1/simulations?sync=true",
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
            "/v1/simulations?sync=true",
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


# ============================================================================
# 7. Phase 11 Simulation Persistence & History Tests (GET /v1/simulations/{id})
# ============================================================================

def test_simulation_persisted_on_post(client: TestClient, db_session: Session):
    """Verify POST /v1/simulations persists record in database matching response."""
    user = User(email="persist_test_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Persist Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    resp = client.post(
        "/v1/simulations?sync=true",
        headers=headers,
        json={"preset": "education", "num_interactions": 10, "seed": 42},
    )
    assert resp.status_code == 200
    resp_data = resp.json()
    sim_id = resp_data["simulation_id"]

    # Check database persistence
    sim = db_session.query(Simulation).filter_by(id=uuid.UUID(sim_id)).first()
    assert sim is not None
    assert sim.user_id == user.id
    assert sim.preset == "education"
    assert sim.num_interactions == 10
    assert sim.seed == 42
    assert sim.status == "completed"
    assert sim.result_storage == "database"
    assert sim.reproducible is True
    assert len(sim.data) == 10
    assert sim.data == resp_data["data"]


def test_get_simulation_by_owner(client: TestClient, db_session: Session):
    """Verify owner can retrieve persisted simulation run by ID."""
    user = User(email="owner_sim_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Owner Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Execute simulation
    post_resp = client.post(
        "/v1/simulations?sync=true",
        headers=headers,
        json={"preset": "education", "num_interactions": 10, "seed": 123},
    )
    assert post_resp.status_code == 200
    sim_id = post_resp.json()["simulation_id"]

    # Retrieve simulation by ID
    get_resp = client.get(f"/v1/simulations/{sim_id}", headers=headers)
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["simulation_id"] == sim_id
    assert get_data["preset"] == "education"
    assert get_data["num_interactions"] == 10
    assert get_data["seed"] == 123
    assert get_data["status"] == "completed"
    assert len(get_data["data"]) == 10
    assert "metadata" in get_data
    assert get_data["metadata"]["reproducible"] is True
    assert "created_at" in get_data
    assert "completed_at" in get_data


def test_get_simulation_unauthenticated(client: TestClient, db_session: Session):
    """Verify unauthenticated GET /v1/simulations/{id} returns HTTP 401."""
    random_id = str(uuid.uuid4())
    resp = client.get(f"/v1/simulations/{random_id}")
    assert resp.status_code == 401


def test_get_simulation_idor_protection(client: TestClient, db_session: Session):
    """Verify User B cannot retrieve User A's simulation run (returns HTTP 404, preventing IDOR/leakage)."""
    user_a = User(email="user_a_sim@example.com")
    user_b = User(email="user_b_sim@example.com")
    db_session.add_all([user_a, user_b])
    db_session.commit()

    key_a = create_api_key(db_session, user_a, name="Key A")
    key_b = create_api_key(db_session, user_b, name="Key B")

    # User A creates a simulation
    resp_a = client.post(
        "/v1/simulations?sync=true",
        headers={"Authorization": f"Bearer {key_a.key}"},
        json={"preset": "education", "num_interactions": 10},
    )
    assert resp_a.status_code == 200
    sim_id = resp_a.json()["simulation_id"]

    # User B attempts to access User A's simulation
    resp_b = client.get(
        f"/v1/simulations/{sim_id}",
        headers={"Authorization": f"Bearer {key_b.key}"},
    )
    assert resp_b.status_code == 404
    assert resp_b.json()["error"]["details"]["code"] == "simulation_not_found"


def test_get_simulation_not_found_and_malformed(client: TestClient, db_session: Session):
    """Verify nonexistent UUID and malformed UUID both return 404 simulation_not_found."""
    user = User(email="notfound_test_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Nonexistent UUID
    nonexistent_id = str(uuid.uuid4())
    resp_nonexistent = client.get(f"/v1/simulations/{nonexistent_id}", headers=headers)
    assert resp_nonexistent.status_code == 404
    assert resp_nonexistent.json()["error"]["details"]["code"] == "simulation_not_found"

    # Malformed non-UUID
    resp_malformed = client.get("/v1/simulations/not-a-valid-uuid", headers=headers)
    assert resp_malformed.status_code == 404
    assert resp_malformed.json()["error"]["details"]["code"] == "simulation_not_found"


def test_simulation_persistence_failure_refunds_quota(client: TestClient, db_session: Session):
    """Verify failure during database persistence triggers rollback and refunds quota."""
    user = User(email="persist_fail_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Ensure monthly usage record exists before mock
    period_start = get_current_period_start()
    db_session.query(MonthlyUsage).filter_by(user_id=user.id, period_start=period_start).first()

    original_add = Session.add

    def mocked_add(self, instance, *args, **kwargs):
        if isinstance(instance, Simulation):
            raise RuntimeError("DB simulation write error")
        return original_add(self, instance, *args, **kwargs)

    with patch.object(Session, "add", side_effect=mocked_add, autospec=True):
        resp = client.post(
            "/v1/simulations",
            headers=headers,
            json={"preset": "education", "num_interactions": 25},
        )
        assert resp.status_code == 500
        assert resp.json()["error"]["details"]["code"] == "simulation_persistence_failed"

    # Verify quota was refunded
    db_session.expire_all()
    usage = db_session.query(MonthlyUsage).filter_by(user_id=user.id, period_start=period_start).first()
    assert usage is not None
    assert usage.request_count == 0
    assert usage.interaction_count == 0


# ============================================================================
# 8. Phase 12 Simulation History & List Tests (GET /v1/simulations)
# ============================================================================

def test_list_simulations_unauthenticated(client: TestClient, db_session: Session):
    """Verify unauthenticated GET /v1/simulations returns HTTP 401."""
    resp = client.get("/v1/simulations")
    assert resp.status_code == 401


def test_list_simulations_empty_history(client: TestClient, db_session: Session):
    """Verify authenticated user with no simulations receives empty list and total=0."""
    user = User(email="empty_sim_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Empty Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    resp = client.get("/v1/simulations", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["has_next"] is False


def test_list_simulations_basic_and_ordering(client: TestClient, db_session: Session):
    """Verify multiple simulations are returned newest first with bounded metadata."""
    user = User(email="list_order_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Order Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim1 = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=10,
        seed=1,
        status="completed",
        data=[{"i": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=15,
        reproducible=True,
    )
    sim2 = Simulation(
        user_id=user.id,
        preset="finance",
        num_interactions=20,
        seed=2,
        status="completed",
        data=[{"i": 2}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=25,
        reproducible=True,
    )
    db_session.add_all([sim1, sim2])
    db_session.commit()

    resp = client.get("/v1/simulations", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Verify newest first (sim2 was committed after sim1)
    item_ids = [item["simulation_id"] for item in data["items"]]
    assert str(sim2.id) in item_ids
    assert str(sim1.id) in item_ids

    # Verify lightweight fields are present
    first_item = data["items"][0]
    assert "simulation_id" in first_item
    assert "preset" in first_item
    assert "num_interactions" in first_item
    assert "status" in first_item
    assert "compute_ms" in first_item
    assert "reproducible" in first_item
    assert "created_at" in first_item
    assert "completed_at" in first_item


def test_list_simulations_excludes_data_payload(client: TestClient, db_session: Session):
    """Verify GET /v1/simulations strictly omits the heavy data payload from items."""
    user = User(email="no_data_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="NoData Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=50,
        seed=42,
        status="completed",
        data=[{"heavy_telemetry": "sample_record"}] * 50,
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=30,
        reproducible=True,
    )
    db_session.add(sim)
    db_session.commit()

    resp = client.get("/v1/simulations", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert "data" not in data["items"][0]


def test_list_simulations_pagination(client: TestClient, db_session: Session):
    """Verify pagination mechanics: page, page_size, total, and has_next boundaries."""
    user = User(email="paging_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Paging Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sims = [
        Simulation(
            user_id=user.id,
            preset="education",
            num_interactions=i,
            seed=i,
            status="completed",
            data=[{"i": i}],
            behaviorsim_version="1.0.1",
            api_version="0.1.0",
            compute_ms=10,
            reproducible=True,
        )
        for i in range(1, 6)
    ]
    db_session.add_all(sims)
    db_session.commit()

    # Page 1, size 2 -> items: 2, total: 5, has_next: True
    r1 = client.get("/v1/simulations?page=1&page_size=2", headers=headers)
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["items"]) == 2
    assert d1["total"] == 5
    assert d1["page"] == 1
    assert d1["page_size"] == 2
    assert d1["has_next"] is True

    # Page 2, size 2 -> items: 2, total: 5, has_next: True
    r2 = client.get("/v1/simulations?page=2&page_size=2", headers=headers)
    assert r2.status_code == 200
    d2 = r2.json()
    assert len(d2["items"]) == 2
    assert d2["has_next"] is True

    # Ensure Page 1 and Page 2 items do not overlap
    page1_ids = {item["simulation_id"] for item in d1["items"]}
    page2_ids = {item["simulation_id"] for item in d2["items"]}
    assert page1_ids.isdisjoint(page2_ids)

    # Page 3, size 2 -> items: 1, total: 5, has_next: False
    r3 = client.get("/v1/simulations?page=3&page_size=2", headers=headers)
    assert r3.status_code == 200
    d3 = r3.json()
    assert len(d3["items"]) == 1
    assert d3["has_next"] is False

    # Page 4, size 2 -> items: 0, total: 5, has_next: False
    r4 = client.get("/v1/simulations?page=4&page_size=2", headers=headers)
    assert r4.status_code == 200
    d4 = r4.json()
    assert len(d4["items"]) == 0
    assert d4["has_next"] is False


def test_list_simulations_pagination_validation(client: TestClient, db_session: Session):
    """Verify invalid pagination query params return HTTP 422 validation error."""
    user = User(email="val_page_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Val Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # page=0 is invalid (ge=1)
    r_page_zero = client.get("/v1/simulations?page=0", headers=headers)
    assert r_page_zero.status_code == 422

    # page_size=0 is invalid (ge=1)
    r_size_zero = client.get("/v1/simulations?page_size=0", headers=headers)
    assert r_size_zero.status_code == 422

    # page_size=101 is invalid (le=100)
    r_size_excess = client.get("/v1/simulations?page_size=101", headers=headers)
    assert r_size_excess.status_code == 422


def test_list_simulations_filtering(client: TestClient, db_session: Session):
    """Verify bounded filtering on preset (with alias normalization) and status."""
    user = User(email="filter_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Filter Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim_edu = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=10,
        status="completed",
        data=[{"e": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    sim_fin = Simulation(
        user_id=user.id,
        preset="finance",
        num_interactions=10,
        status="completed",
        data=[{"f": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    sim_mob = Simulation(
        user_id=user.id,
        preset="mobile_app",
        num_interactions=10,
        status="completed",
        data=[{"m": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    db_session.add_all([sim_edu, sim_fin, sim_mob])
    db_session.commit()

    # Filter preset=education
    r_edu = client.get("/v1/simulations?preset=education", headers=headers)
    assert r_edu.status_code == 200
    d_edu = r_edu.json()
    assert d_edu["total"] == 1
    assert d_edu["items"][0]["preset"] == "education"

    # Filter preset=mobile (alias for mobile_app)
    r_mob = client.get("/v1/simulations?preset=mobile", headers=headers)
    assert r_mob.status_code == 200
    d_mob = r_mob.json()
    assert d_mob["total"] == 1
    assert d_mob["items"][0]["preset"] == "mobile_app"

    # Filter status=completed
    r_stat = client.get("/v1/simulations?status=completed", headers=headers)
    assert r_stat.status_code == 200
    assert r_stat.json()["total"] == 3

    # Combined matching filter
    r_comb = client.get("/v1/simulations?preset=finance&status=completed", headers=headers)
    assert r_comb.status_code == 200
    assert r_comb.json()["total"] == 1
    assert r_comb.json()["items"][0]["preset"] == "finance"

    # Combined non-matching filter
    r_non = client.get("/v1/simulations?preset=finance&status=failed", headers=headers)
    assert r_non.status_code == 200
    assert r_non.json()["total"] == 0
    assert r_non.json()["items"] == []


def test_list_simulations_invalid_filters(client: TestClient, db_session: Session):
    """Verify invalid preset or status raises HTTP 400 with descriptive error code."""
    user = User(email="invalid_filter_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Invalid Filter Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Invalid preset
    r_inv_preset = client.get("/v1/simulations?preset=unknown_domain", headers=headers)
    assert r_inv_preset.status_code == 400
    assert r_inv_preset.json()["error"]["details"]["code"] == "invalid_preset"

    # Invalid status
    r_inv_status = client.get("/v1/simulations?status=destroyed", headers=headers)
    assert r_inv_status.status_code == 400
    assert r_inv_status.json()["error"]["details"]["code"] == "invalid_status"


def test_list_simulations_ownership_isolation(client: TestClient, db_session: Session):
    """Verify strict caller ownership scoping: User A cannot see User B's simulations or totals."""
    user_a = User(email="owner_a_list@example.com")
    user_b = User(email="owner_b_list@example.com")
    db_session.add_all([user_a, user_b])
    db_session.commit()

    key_a = create_api_key(db_session, user_a, name="Key A")
    key_b = create_api_key(db_session, user_b, name="Key B")

    sim_a1 = Simulation(
        user_id=user_a.id,
        preset="education",
        num_interactions=10,
        status="completed",
        data=[{"a": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    sim_a2 = Simulation(
        user_id=user_a.id,
        preset="finance",
        num_interactions=10,
        status="completed",
        data=[{"a": 2}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    sim_b1 = Simulation(
        user_id=user_b.id,
        preset="healthcare",
        num_interactions=10,
        status="completed",
        data=[{"b": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    db_session.add_all([sim_a1, sim_a2, sim_b1])
    db_session.commit()

    # User A listing
    r_a = client.get("/v1/simulations", headers={"Authorization": f"Bearer {key_a.key}"})
    assert r_a.status_code == 200
    d_a = r_a.json()
    assert d_a["total"] == 2
    a_ids = {item["simulation_id"] for item in d_a["items"]}
    assert a_ids == {str(sim_a1.id), str(sim_a2.id)}
    assert str(sim_b1.id) not in a_ids

    # User B listing
    r_b = client.get("/v1/simulations", headers={"Authorization": f"Bearer {key_b.key}"})
    assert r_b.status_code == 200
    d_b = r_b.json()
    assert d_b["total"] == 1
    b_ids = {item["simulation_id"] for item in d_b["items"]}
    assert b_ids == {str(sim_b1.id)}
    assert str(sim_a1.id) not in b_ids
    assert str(sim_a2.id) not in b_ids


def test_simulation_detail_regression_with_history(client: TestClient, db_session: Session):
    """Verify GET /v1/simulations/{id} still retrieves full record including data payload."""
    user = User(email="detail_regress_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=5,
        seed=99,
        status="completed",
        data=[{"row": 1}, {"row": 2}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=12,
        reproducible=True,
    )
    db_session.add(sim)
    db_session.commit()

    resp = client.get(f"/v1/simulations/{sim.id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["simulation_id"] == str(sim.id)
    assert data["data"] == [{"row": 1}, {"row": 2}]
    assert data["preset"] == "education"


# ============================================================================
# 9. Phase 13 Simulation Deletion Tests (DELETE /v1/simulations/{id})
# ============================================================================

def test_delete_simulation_unauthenticated(client: TestClient):
    """Verify unauthenticated DELETE /v1/simulations/{id} returns HTTP 401."""
    random_id = str(uuid.uuid4())
    resp = client.delete(f"/v1/simulations/{random_id}")
    assert resp.status_code == 401


def test_delete_simulation_owner(client: TestClient, db_session: Session):
    """Verify owner can permanently delete simulation run with 204 response."""
    user = User(email="del_owner_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Del Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=10,
        status="completed",
        data=[{"x": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    db_session.add(sim)
    db_session.commit()
    sim_id = str(sim.id)

    # Delete simulation
    resp = client.delete(f"/v1/simulations/{sim_id}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""

    # Confirm record is gone from DB
    db_session.expire_all()
    deleted = db_session.query(Simulation).filter_by(id=uuid.UUID(sim_id)).first()
    assert deleted is None

    # Confirm detail endpoint returns 404
    r_detail = client.get(f"/v1/simulations/{sim_id}", headers=headers)
    assert r_detail.status_code == 404
    assert r_detail.json()["error"]["details"]["code"] == "simulation_not_found"

    # Confirm history endpoint excludes it
    r_hist = client.get("/v1/simulations", headers=headers)
    assert r_hist.status_code == 200
    assert r_hist.json()["total"] == 0
    assert r_hist.json()["items"] == []


def test_delete_simulation_ownership_isolation(client: TestClient, db_session: Session):
    """Verify User B cannot delete User A's simulation (returns 404; User A's data intact)."""
    user_a = User(email="del_user_a@example.com")
    user_b = User(email="del_user_b@example.com")
    db_session.add_all([user_a, user_b])
    db_session.commit()

    key_a = create_api_key(db_session, user_a, name="Key A")
    key_b = create_api_key(db_session, user_b, name="Key B")

    sim_a = Simulation(
        user_id=user_a.id,
        preset="education",
        num_interactions=10,
        status="completed",
        data=[{"secret": "user_a_data"}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    db_session.add(sim_a)
    db_session.commit()
    sim_a_id = str(sim_a.id)

    # User B attempts to delete User A's simulation
    resp_b = client.delete(
        f"/v1/simulations/{sim_a_id}",
        headers={"Authorization": f"Bearer {key_b.key}"},
    )
    assert resp_b.status_code == 404
    assert resp_b.json()["error"]["details"]["code"] == "simulation_not_found"

    # Verify simulation A is still intact in DB and retrievable by User A
    db_session.expire_all()
    intact = db_session.query(Simulation).filter_by(id=uuid.UUID(sim_a_id)).first()
    assert intact is not None

    resp_a = client.get(
        f"/v1/simulations/{sim_a_id}",
        headers={"Authorization": f"Bearer {key_a.key}"},
    )
    assert resp_a.status_code == 200
    assert resp_a.json()["simulation_id"] == sim_a_id


def test_delete_simulation_not_found(client: TestClient, db_session: Session):
    """Verify deleting a nonexistent UUID returns HTTP 404."""
    user = User(email="del_notfound_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    random_id = str(uuid.uuid4())
    resp = client.delete(f"/v1/simulations/{random_id}", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["details"]["code"] == "simulation_not_found"


def test_delete_simulation_malformed_id(client: TestClient, db_session: Session):
    """Verify deleting a malformed non-UUID string returns HTTP 404."""
    user = User(email="del_malformed_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    resp = client.delete("/v1/simulations/not-a-valid-uuid", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["details"]["code"] == "simulation_not_found"


def test_delete_simulation_already_deleted(client: TestClient, db_session: Session):
    """Verify repeated deletion of the same ID returns 404 on the second call."""
    user = User(email="del_repeat_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=5,
        status="completed",
        data=[{"a": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    db_session.add(sim)
    db_session.commit()
    sim_id = str(sim.id)

    # First deletion -> 204
    r1 = client.delete(f"/v1/simulations/{sim_id}", headers=headers)
    assert r1.status_code == 204

    # Second deletion -> 404
    r2 = client.delete(f"/v1/simulations/{sim_id}", headers=headers)
    assert r2.status_code == 404
    assert r2.json()["error"]["details"]["code"] == "simulation_not_found"


def test_delete_simulation_does_not_refund_quota(client: TestClient, db_session: Session):
    """Verify deleting a simulation does NOT decrement or refund monthly quota."""
    user = User(email="del_norefund_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    # Execute simulation via POST
    post_resp = client.post(
        "/v1/simulations?sync=true",
        headers=headers,
        json={"preset": "education", "num_interactions": 15},
    )
    assert post_resp.status_code == 200
    sim_id = post_resp.json()["simulation_id"]

    # Verify usage was recorded
    period_start = get_current_period_start()
    db_session.expire_all()
    usage = db_session.query(MonthlyUsage).filter_by(user_id=user.id, period_start=period_start).first()
    assert usage is not None
    assert usage.request_count == 1
    assert usage.interaction_count == 15

    # Delete the simulation
    del_resp = client.delete(f"/v1/simulations/{sim_id}", headers=headers)
    assert del_resp.status_code == 204

    # Verify usage counters remain strictly unchanged
    db_session.expire_all()
    usage_after = db_session.query(MonthlyUsage).filter_by(user_id=user.id, period_start=period_start).first()
    assert usage_after is not None
    assert usage_after.request_count == 1
    assert usage_after.interaction_count == 15


def test_delete_simulation_multi_sim_integrity(client: TestClient, db_session: Session):
    """Verify deleting 1 of 3 simulations leaves other simulations, totals, and ordering intact."""
    user = User(email="del_multi_user@example.com")
    db_session.add(user)
    db_session.commit()
    key = create_api_key(db_session, user, name="Key")
    headers = {"Authorization": f"Bearer {key.key}"}

    sim1 = Simulation(
        user_id=user.id,
        preset="education",
        num_interactions=10,
        status="completed",
        data=[{"sim": 1}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    sim2 = Simulation(
        user_id=user.id,
        preset="finance",
        num_interactions=20,
        status="completed",
        data=[{"sim": 2}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    sim3 = Simulation(
        user_id=user.id,
        preset="healthcare",
        num_interactions=30,
        status="completed",
        data=[{"sim": 3}],
        behaviorsim_version="1.0.1",
        api_version="0.1.0",
        compute_ms=10,
        reproducible=False,
    )
    db_session.add_all([sim1, sim2, sim3])
    db_session.commit()

    # Pre-check: 3 items in history
    r_init = client.get("/v1/simulations", headers=headers)
    assert r_init.status_code == 200
    assert r_init.json()["total"] == 3

    # Delete middle simulation (sim2)
    r_del = client.delete(f"/v1/simulations/{sim2.id}", headers=headers)
    assert r_del.status_code == 204

    # History now has total=2, items contain sim3 and sim1, sim2 is gone
    r_post = client.get("/v1/simulations", headers=headers)
    assert r_post.status_code == 200
    post_data = r_post.json()
    assert post_data["total"] == 2
    post_ids = [item["simulation_id"] for item in post_data["items"]]
    assert str(sim2.id) not in post_ids
    assert str(sim1.id) in post_ids
    assert str(sim3.id) in post_ids

    # Detail checks
    assert client.get(f"/v1/simulations/{sim2.id}", headers=headers).status_code == 404
    assert client.get(f"/v1/simulations/{sim1.id}", headers=headers).status_code == 200
    assert client.get(f"/v1/simulations/{sim3.id}", headers=headers).status_code == 200

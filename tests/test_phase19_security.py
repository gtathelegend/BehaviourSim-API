"""Comprehensive security, IDOR, authorization, and abuse-resistance tests for Phase 19."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.diagnostics import get_cached_queue_counts, reset_diagnostics_cache
from app.core.config import get_settings
from app.core.rate_limit import default_rate_limiter
from app.core.security import generate_api_key, hash_session_token
from app.db.models.api_key import APIKey
from app.db.models.plan import Plan
from app.db.models.session import UserSession
from app.db.models.simulation import Simulation
from app.db.models.user import User
from app.services.api_key import create_api_key, revoke_api_key
from app.services.simulation_job import STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING
from app.services.usage import get_current_period_start, get_or_create_monthly_usage
from app.services.worker import get_user_concurrency_capacity


def _create_test_plan(db: Session, name: str = "SecPlan", max_keys: int = 5, max_sims: int = 2, req_per_min: int = 20) -> Plan:
    plan = db.query(Plan).filter_by(name=name).first()
    if not plan:
        plan = Plan(
            name=name,
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=req_per_min,
            max_concurrent_simulations=max_sims,
            max_api_keys=max_keys,
        )
        db.add(plan)
        db.commit()
    else:
        plan.requests_per_minute = req_per_min
        db.commit()
    return plan


def _create_user_with_key(db: Session, email: str, plan: Plan) -> tuple[User, str, APIKey]:
    user = User(email=email, is_active=True, plan_id=plan.id)
    user.plan = plan
    db.add(user)
    db.commit()
    db.refresh(user)

    created_key = create_api_key(db=db, user=user, name="test-key")
    db_key = db.get(APIKey, created_key.id)
    return user, created_key.key, db_key


# ==============================================================================
# 1. IDOR / COMPOUND AUTHORIZATION TESTS
# ==============================================================================

def test_cross_user_simulation_access_prevented(client: TestClient, db_session: Session):
    """User A cannot access User B's simulation; returns 404 simulation_not_found."""
    plan = _create_test_plan(db_session, "IDORPlan")
    user_a, key_a, _ = _create_user_with_key(db_session, "user_a@test.com", plan)
    user_b, key_b, _ = _create_user_with_key(db_session, "user_b@test.com", plan)

    # User B creates a simulation
    sim_b = Simulation(
        id=uuid.uuid4(),
        user_id=user_b.id,
        preset="education",
        num_interactions=100,
        status=STATUS_COMPLETED,
        compute_ms=45,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
    )
    db_session.add(sim_b)
    db_session.commit()

    # User A tries to GET User B's simulation
    res = client.get(
        f"/v1/simulations/{sim_b.id}",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert res.status_code == 404
    body = res.json()
    assert body["error"]["details"]["code"] == "simulation_not_found"
    assert "not found" in body["error"]["message"].lower()


def test_cross_user_simulation_deletion_prevented(client: TestClient, db_session: Session):
    """User A cannot delete User B's simulation; returns 404 simulation_not_found."""
    plan = _create_test_plan(db_session, "IDORDelPlan")
    user_a, key_a, _ = _create_user_with_key(db_session, "user_adel@test.com", plan)
    user_b, key_b, _ = _create_user_with_key(db_session, "user_bdel@test.com", plan)

    sim_b = Simulation(
        id=uuid.uuid4(),
        user_id=user_b.id,
        preset="education",
        num_interactions=100,
        status=STATUS_PENDING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
    )
    db_session.add(sim_b)
    db_session.commit()

    res = client.delete(
        f"/v1/simulations/{sim_b.id}",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert res.status_code == 404
    assert res.json()["error"]["details"]["code"] == "simulation_not_found"

    # Verify sim_b is still intact in the database
    db_session.refresh(sim_b)
    assert sim_b is not None


def test_cross_user_api_key_revocation_prevented(client: TestClient, db_session: Session):
    """User A cannot delete/revoke User B's API key."""
    plan = _create_test_plan(db_session, "IDORKeyPlan")
    user_a, key_a, _ = _create_user_with_key(db_session, "user_akey@test.com", plan)
    user_b, key_b, db_key_b = _create_user_with_key(db_session, "user_bkey@test.com", plan)

    res = client.delete(
        f"/v1/api-keys/{db_key_b.id}",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert res.status_code == 404

    # Verify Key B remains active
    db_session.refresh(db_key_b)
    assert db_key_b.is_active is True
    assert db_key_b.revoked_at is None


def test_simulation_history_strictly_scoped_to_caller(client: TestClient, db_session: Session):
    """User A's simulation history never enumerates User B's simulations."""
    plan = _create_test_plan(db_session, "ScopePlan")
    user_a, key_a, _ = _create_user_with_key(db_session, "user_ascope@test.com", plan)
    user_b, key_b, _ = _create_user_with_key(db_session, "user_bscope@test.com", plan)

    now = datetime.now(timezone.utc)
    sim_a = Simulation(id=uuid.uuid4(), user_id=user_a.id, preset="education", num_interactions=10, status=STATUS_COMPLETED, compute_ms=10, created_at=now, updated_at=now, behaviorsim_version="1.0.1", api_version="1.0.0")
    sim_b = Simulation(id=uuid.uuid4(), user_id=user_b.id, preset="finance", num_interactions=20, status=STATUS_COMPLETED, compute_ms=20, created_at=now, updated_at=now, behaviorsim_version="1.0.1", api_version="1.0.0")
    db_session.add_all([sim_a, sim_b])
    db_session.commit()

    res_a = client.get("/v1/simulations", headers={"Authorization": f"Bearer {key_a}"})
    assert res_a.status_code == 200
    items_a = res_a.json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["simulation_id"] == str(sim_a.id)

    res_b = client.get("/v1/simulations", headers={"Authorization": f"Bearer {key_b}"})
    assert res_b.status_code == 200
    items_b = res_b.json()["items"]
    assert len(items_b) == 1
    assert items_b[0]["simulation_id"] == str(sim_b.id)


# ==============================================================================
# 2. SIMULATION ID SECURITY & ORACLE ELIMINATION
# ==============================================================================

@pytest.mark.parametrize("malformed_id", [
    "not-a-valid-uuid",
    "12345",
    "' OR 1=1 --",
    "etc_passwd_traversal",
    "g" * 36,
    "123e4567-e89b-12d3-a456-42661417400g",
    "x" * 256,
])
def test_malformed_simulation_id_returns_404_not_found(client: TestClient, db_session: Session, malformed_id: str):
    """Malformed simulation IDs return identical 404 simulation_not_found without leaking parsing errors."""
    plan = _create_test_plan(db_session, "FuzzPlan")
    _, key, _ = _create_user_with_key(db_session, "fuzz_user@test.com", plan)

    res = client.get(f"/v1/simulations/{malformed_id}", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 404
    body = res.json()
    assert body["error"]["details"]["code"] == "simulation_not_found"


def test_oracle_elimination_nonexistent_vs_unauthorized_uuid(client: TestClient, db_session: Session):
    """Nonexistent UUID and foreign user's UUID return identical status and error payload (zero oracle)."""
    plan = _create_test_plan(db_session, "OraclePlan")
    user_a, key_a, _ = _create_user_with_key(db_session, "oracle_a@test.com", plan)
    user_b, key_b, _ = _create_user_with_key(db_session, "oracle_b@test.com", plan)

    # Real simulation belonging to user B
    foreign_uuid = uuid.uuid4()
    sim_b = Simulation(id=foreign_uuid, user_id=user_b.id, preset="education", num_interactions=10, status=STATUS_COMPLETED, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), behaviorsim_version="1.0.1", api_version="1.0.0")
    db_session.add(sim_b)
    db_session.commit()

    # Random nonexistent UUID
    nonexistent_uuid = uuid.uuid4()

    res_foreign = client.get(f"/v1/simulations/{foreign_uuid}", headers={"Authorization": f"Bearer {key_a}"})
    res_nonexistent = client.get(f"/v1/simulations/{nonexistent_uuid}", headers={"Authorization": f"Bearer {key_a}"})

    assert res_foreign.status_code == 404
    assert res_nonexistent.status_code == 404
    # Status, code, and response structure must be identical
    assert res_foreign.json()["error"]["details"]["code"] == res_nonexistent.json()["error"]["details"]["code"]
    assert res_foreign.json()["error"]["details"]["code"] == "simulation_not_found"


# ==============================================================================
# 3. API-KEY SECURITY & MULTI-KEY RATE LIMIT SHARING
# ==============================================================================

def test_revoked_api_key_immediately_rejected(client: TestClient, db_session: Session):
    """Revoking an API key immediately causes 401 Unauthorized on subsequent requests."""
    plan = _create_test_plan(db_session, "RevokePlan")
    user, raw_key, db_key = _create_user_with_key(db_session, "rev_user@test.com", plan)

    # 1. Works before revocation
    res1 = client.get("/v1/account", headers={"Authorization": f"Bearer {raw_key}"})
    assert res1.status_code == 200

    # 2. Revoke key
    revoke_api_key(db=db_session, user=user, key_id=db_key.id)

    # 3. Immediately rejected
    res2 = client.get("/v1/account", headers={"Authorization": f"Bearer {raw_key}"})
    assert res2.status_code == 401
    assert "revoked" in res2.json()["error"]["message"].lower() or "invalid" in res2.json()["error"]["message"].lower()


def test_multiple_api_keys_share_account_rate_limit(client: TestClient, db_session: Session):
    """Multiple API keys belonging to the same user share the exact same rate-limiting pool."""
    default_rate_limiter.reset()
    plan = _create_test_plan(db_session, "RateLimitMultiKeyPlan", req_per_min=2)

    user, key_1, _ = _create_user_with_key(db_session, "multikey@test.com", plan)
    # Create second key for same user
    created_2 = create_api_key(db=db_session, user=user, name="key-2")
    key_2 = created_2.key

    # Request 1 with Key 1 (1/2 consumed)
    r1 = client.get("/v1/usage", headers={"Authorization": f"Bearer {key_1}"})
    assert r1.status_code == 200

    # Request 2 with Key 2 (2/2 consumed)
    r2 = client.get("/v1/usage", headers={"Authorization": f"Bearer {key_2}"})
    assert r2.status_code == 200

    # Request 3 with Key 2 is blocked (429 Rate Limit Exceeded)
    r3 = client.get("/v1/usage", headers={"Authorization": f"Bearer {key_2}"})
    assert r3.status_code == 429
    assert r3.json()["error"]["details"]["code"] == "rate_limit_exceeded"

    # Request 4 with Key 1 is also blocked
    r4 = client.get("/v1/usage", headers={"Authorization": f"Bearer {key_1}"})
    assert r4.status_code == 429


# ==============================================================================
# 4. OAUTH STATE & CSRF TESTS
# ==============================================================================

def test_oauth_callback_requires_matching_state_cookie(client: TestClient):
    """Callback without matching state cookie is rejected with 400 invalid_oauth_state."""
    res = client.get("/v1/auth/google/callback?code=mock_code&state=injected_state")
    assert res.status_code == 400
    assert res.json()["error"]["details"]["code"] == "invalid_oauth_state"


def test_oauth_callback_rejects_state_mismatch(client: TestClient):
    """Callback with mismatched state query param and state cookie is rejected."""
    client.cookies.set("behaviorsim_oauth_state", "real_state_123")
    res = client.get("/v1/auth/google/callback?code=mock_code&state=attacker_state_456")
    assert res.status_code == 400
    assert res.json()["error"]["details"]["code"] == "invalid_oauth_state"


# ==============================================================================
# 5. SESSION SECURITY & LOGOUT
# ==============================================================================

def test_session_logout_invalidates_token(client: TestClient, db_session: Session):
    """POST /v1/auth/logout invalidates the session and deletes cookie."""
    plan = _create_test_plan(db_session, "SessPlan")
    user = User(email="logout_user@test.com", is_active=True, plan_id=plan.id)
    db_session.add(user)
    db_session.commit()

    raw_token = "bs_sess_test1234567890abcdef12345678"
    token_hash = hash_session_token(raw_token)
    session = UserSession(
        id=uuid.uuid4(),
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    db_session.add(session)
    db_session.commit()

    # Authenticated request with session cookie
    client.cookies.set("behaviorsim_session", raw_token)
    acc_res = client.get("/v1/account")
    assert acc_res.status_code == 200

    # Logout
    logout_res = client.post("/v1/auth/logout")
    assert logout_res.status_code == 200

    # Verify session is revoked in DB
    db_session.refresh(session)
    assert session.revoked_at is not None

    # Subsequent request fails
    post_res = client.get("/v1/account", headers={"Authorization": f"Bearer {raw_token}"})
    assert post_res.status_code == 401


# ==============================================================================
# 6. QUOTA ABUSE & REVENUE LEAK PREVENTION
# ==============================================================================

def test_deleting_completed_simulation_does_not_refund_quota(client: TestClient, db_session: Session):
    """Deleting a completed simulation permanently deletes the row but NEVER refunds quota."""
    plan = _create_test_plan(db_session, "QuotaDelPlan")
    user, key, _ = _create_user_with_key(db_session, "quotadel@test.com", plan)

    # Set usage
    period_start = get_current_period_start()
    usage = get_or_create_monthly_usage(db_session, user.id, period_start)
    usage.request_count = 5
    usage.interaction_count = 500
    db_session.commit()

    # Create completed simulation
    sim = Simulation(
        id=uuid.uuid4(),
        user_id=user.id,
        preset="education",
        num_interactions=100,
        status=STATUS_COMPLETED,
        compute_ms=50,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
    )
    db_session.add(sim)
    db_session.commit()

    # Delete completed simulation
    res = client.delete(f"/v1/simulations/{sim.id}", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 204

    # Verify usage was NOT decremented
    db_session.refresh(usage)
    assert usage.request_count == 5
    assert usage.interaction_count == 500


def test_cannot_delete_running_simulation(client: TestClient, db_session: Session):
    """Deleting a currently running simulation returns 409 Conflict, preventing worker state corruption."""
    plan = _create_test_plan(db_session, "RunningDelPlan")
    user, key, _ = _create_user_with_key(db_session, "runningdel@test.com", plan)

    sim = Simulation(
        id=uuid.uuid4(),
        user_id=user.id,
        preset="healthcare",
        num_interactions=100,
        status=STATUS_RUNNING,
        worker_id="active-worker",
        claimed_at=datetime.now(timezone.utc),
        heartbeat_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
    )
    db_session.add(sim)
    db_session.commit()

    res = client.delete(f"/v1/simulations/{sim.id}", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 409
    assert res.json()["error"]["details"]["code"] == "cannot_delete_running_simulation"


# ==============================================================================
# 7. INPUT VALIDATION & PAYLOAD CEILING
# ==============================================================================

def test_request_body_limit_enforced(client: TestClient, db_session: Session):
    """Payloads exceeding 1MB ceiling are rejected with 413 payload_too_large."""
    plan = _create_test_plan(db_session, "BodyLimitPlan")
    _, key, _ = _create_user_with_key(db_session, "bodylimit@test.com", plan)

    # 1.5 MB body
    large_payload = {"preset": "education", "num_interactions": 10, "profile": "A" * (1_500_000)}
    body_bytes = json.dumps(large_payload).encode("utf-8")

    res = client.post(
        "/v1/simulations",
        content=body_bytes,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Content-Length": str(len(body_bytes)),
        },
    )
    assert res.status_code == 413
    assert res.json()["error"]["details"]["code"] == "payload_too_large"


def test_extra_fields_forbidden_in_simulation_request(client: TestClient, db_session: Session):
    """Extra unexpected JSON fields in simulation request trigger 422 Unprocessable Entity."""
    plan = _create_test_plan(db_session, "ExtraFieldPlan")
    _, key, _ = _create_user_with_key(db_session, "extrafield@test.com", plan)

    res = client.post(
        "/v1/simulations",
        json={"preset": "education", "num_interactions": 10, "malicious_injected_field": "exploit"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert res.status_code == 422


# ==============================================================================
# 8. SECURITY HEADERS & INFORMATION DISCLOSURE
# ==============================================================================

def test_security_headers_present_on_all_responses(client: TestClient):
    """All responses include standard security headers including Permissions-Policy and CSP."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]
    assert "camera=()" in res.headers["Permissions-Policy"]


def test_errors_omit_stack_traces_and_internal_paths(client: TestClient, db_session: Session):
    """Error responses return clean JSON envelopes without tracebacks, SQL statements, or filesystem paths."""
    res = client.get("/v1/simulations/invalid-path-id")
    assert res.status_code == 401  # Unauthenticated
    text = res.text
    assert "Traceback" not in text
    assert "File \"" not in text
    assert "SELECT" not in text


# ==============================================================================
# 9. DIAGNOSTICS CACHING & INACTIVE USER WORKER CHECKS
# ==============================================================================

def test_diagnostics_queue_caching_prevents_db_flood(db_session: Session):
    """Cached queue counts return within TTL without hitting database on every invocation."""
    reset_diagnostics_cache()

    # Initial query
    p1, r1 = get_cached_queue_counts(db_session, ttl=5.0)

    # Mock db.execute to verify second call within TTL does not query database
    with patch.object(db_session, "execute") as mock_exec:
        p2, r2 = get_cached_queue_counts(db_session, ttl=5.0)
        assert mock_exec.call_count == 0
        assert p1 == p2
        assert r1 == r2


def test_deactivated_user_has_zero_concurrency_capacity(db_session: Session):
    """Worker get_user_concurrency_capacity returns False for deactivated users."""
    plan = _create_test_plan(db_session, "DeactPlan")
    user = User(email="deact_worker@test.com", is_active=False, plan_id=plan.id)
    db_session.add(user)
    db_session.commit()

    capacity = get_user_concurrency_capacity(db_session, user.id)
    assert capacity is False

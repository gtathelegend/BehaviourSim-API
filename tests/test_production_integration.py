"""Phase 7 Production-Like End-to-End Integration Test Suite.

Validates all 26 production-like operational and behavioral invariants:
 1. Application startup / lifespan
 2. Database initialization & tables
 3. Account creation
 4. Authentication
 5. API-key creation (via HTTP endpoint)
 6. API-key authentication
 7. Account retrieval (/v1/account)
 8. Usage retrieval (/v1/usage)
 9. Preset discovery (/v1/presets)
10. Preset metadata (/v1/presets/{preset})
11. Simulation execution (/v1/simulations)
12. Deterministic repeat simulation with identical seed
13. Successful usage accounting
14. Failed simulation/refund behavior
15. API-key revocation (/v1/api-keys/{key_id})
16. Revoked-key rejection
17. Session authentication
18. Logout (/v1/auth/logout)
19. Post-logout rejection
20. /health liveness probe
21. /ready readiness probe
22. Malformed / oversized requests
23. Structured error behavior
24. Request ID propagation
25. Security headers
26. CORS behavior
"""

import uuid
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from app.auth.service import create_user_session
from app.db.models.user import User
from app.main import create_app
from app.services.plan import get_or_create_free_plan


@pytest.fixture
def prod_client(db_session):
    """Test client with database session overridden and server exceptions not re-raised."""
    from app.db.session import get_db

    app = create_app()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


def test_production_lifecycle_all_26_invariants(prod_client, db_session):
    """Exhaustive test executing all 26 validation invariants in a single unified integration sequence."""

    # 1. Application startup & lifespan: prod_client is initialized via lifespan context
    assert prod_client.app is not None

    # 2. Database initialization / tables: Verify User and Plan seeding
    plan = get_or_create_free_plan(db_session)
    assert plan.name == "free"
    assert plan.max_api_keys == 1

    # 20. /health liveness probe (lightweight, zero-dep)
    health_resp = prod_client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["status"] == "ok"

    # 21. /ready readiness probe (live DB connectivity)
    ready_resp = prod_client.get("/ready")
    assert ready_resp.status_code == 200
    assert ready_resp.json()["status"] == "ready"

    # 25. Security headers verification on public probe
    assert health_resp.headers["X-Content-Type-Options"] == "nosniff"
    assert health_resp.headers["X-Frame-Options"] == "DENY"
    assert health_resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in health_resp.headers["Content-Security-Policy"]

    # 24. Request ID propagation
    custom_req_id = "test-prod-request-id-12345"
    id_resp = prod_client.get("/health", headers={"X-Request-ID": custom_req_id})
    assert id_resp.headers.get("X-Request-ID") == custom_req_id

    # 26. CORS behavior: Valid origin vs unapproved origin
    cors_resp = prod_client.options(
        "/v1/presets",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert cors_resp.headers.get("access-control-allow-origin") == "http://localhost:3000"

    bad_cors_resp = prod_client.options(
        "/v1/presets",
        headers={
            "Origin": "https://malicious-site.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert bad_cors_resp.headers.get("access-control-allow-origin") is None

    # 9. Preset discovery (/v1/presets)
    presets_resp = prod_client.get("/v1/presets")
    assert presets_resp.status_code == 200
    presets_data = presets_resp.json()
    preset_names = [p["name"] for p in presets_data]
    assert "education" in preset_names
    assert "finance" in preset_names

    # 10. Preset metadata inspection (/v1/presets/{preset})
    preset_meta = prod_client.get("/v1/presets/education")
    assert preset_meta.status_code == 200
    assert preset_meta.json()["default_profile"] == "average"

    # 3. Account creation in database
    user = User(
        email="production_user@example.com",
        display_name="Production Candidate",
        is_active=True,
        plan_id=plan.id,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # 17. Session authentication & creation
    session_obj, raw_session_token = create_user_session(db_session, user)

    # 4. Authentication via Session Token in Authorization Bearer header
    sess_auth_headers = {"Authorization": f"Bearer {raw_session_token}"}

    # 7. Account retrieval (/v1/account)
    acc_resp = prod_client.get("/v1/account", headers=sess_auth_headers)
    assert acc_resp.status_code == 200
    acc_data = acc_resp.json()
    assert acc_data["email"] == "production_user@example.com"
    assert acc_data["plan"] == "free"

    # 5. API-key creation via HTTP endpoint (/v1/api-keys)
    key_create_resp = prod_client.post(
        "/v1/api-keys",
        headers=sess_auth_headers,
        json={"name": "Production Deploy Key"},
    )
    assert key_create_resp.status_code == 201
    key_created = key_create_resp.json()
    assert "key" in key_created
    raw_api_key = key_created["key"]
    api_key_id = key_created["id"]
    assert raw_api_key.startswith("bs_live_")

    # Verify key list contains metadata without raw secret
    key_list_resp = prod_client.get("/v1/api-keys", headers=sess_auth_headers)
    assert key_list_resp.status_code == 200
    assert len(key_list_resp.json()) == 1
    assert key_list_resp.json()[0]["id"] == api_key_id
    assert "key" not in key_list_resp.json()[0]

    # Verify Free plan cap enforcement (max_api_keys = 1)
    cap_resp = prod_client.post(
        "/v1/api-keys",
        headers=sess_auth_headers,
        json={"name": "Second Key Should Fail"},
    )
    assert cap_resp.status_code == 403
    assert cap_resp.json()["error"]["details"]["code"] == "plan_limit_exceeded"

    # 6. API-key authentication
    api_auth_headers = {"Authorization": f"Bearer {raw_api_key}"}
    acc_via_key = prod_client.get("/v1/account", headers=api_auth_headers)
    assert acc_via_key.status_code == 200

    # 8. Usage retrieval (/v1/usage)
    usage_init = prod_client.get("/v1/usage", headers=api_auth_headers)
    assert usage_init.status_code == 200
    assert usage_init.json()["usage"]["requests"] == 0
    assert usage_init.json()["usage"]["interactions"] == 0

    # Rate limiting verification (5 requests/minute on Free plan)
    # Endpoints enforcing check_rate_limit are /v1/usage and /v1/simulations.
    # Make 4 more requests to /v1/usage (reaching total 5)
    for _ in range(4):
        resp = prod_client.get("/v1/usage", headers=api_auth_headers)
        assert resp.status_code == 200

    # The 6th request to a rate-limited endpoint must trigger HTTP 429 with Retry-After
    rate_limited_resp = prod_client.get("/v1/usage", headers=api_auth_headers)
    assert rate_limited_resp.status_code == 429
    assert rate_limited_resp.json()["error"]["details"]["code"] == "rate_limit_exceeded"
    assert "Retry-After" in rate_limited_resp.headers

    # Reset rate limiter to proceed with simulation and usage accounting workflow
    from app.core.rate_limit import default_rate_limiter
    default_rate_limiter.reset()

    # 11. Simulation execution (/v1/simulations)
    sim_resp_1 = prod_client.post(
        "/v1/simulations",
        headers=api_auth_headers,
        json={
            "preset": "education",
            "num_interactions": 20,
            "seed": 100,
        },
    )
    assert sim_resp_1.status_code == 200
    sim_data_1 = sim_resp_1.json()
    assert len(sim_data_1["data"]) == 20
    assert sim_data_1["metadata"]["reproducible"] is True
    assert sim_data_1["seed"] == 100

    # 12. Deterministic repeat simulation with identical seed
    sim_resp_2 = prod_client.post(
        "/v1/simulations",
        headers=api_auth_headers,
        json={
            "preset": "education",
            "num_interactions": 20,
            "seed": 100,
        },
    )
    assert sim_resp_2.status_code == 200
    sim_data_2 = sim_resp_2.json()
    assert sim_data_1["data"] == sim_data_2["data"]

    # 13. Successful usage accounting check
    usage_after_sims = prod_client.get("/v1/usage", headers=api_auth_headers)
    assert usage_after_sims.json()["usage"]["requests"] == 2
    assert usage_after_sims.json()["usage"]["interactions"] == 40

    # 14. Failed simulation/refund behavior
    with patch("app.services.simulation.Simulator.from_preset", side_effect=RuntimeError("Engine crash")):
        fail_sim_resp = prod_client.post(
            "/v1/simulations",
            headers=api_auth_headers,
            json={
                "preset": "education",
                "num_interactions": 15,
                "seed": 999,
            },
        )
        assert fail_sim_resp.status_code == 500

    # Verify quota was refunded after failure
    usage_after_refund = prod_client.get("/v1/usage", headers=api_auth_headers)
    assert usage_after_refund.json()["usage"]["requests"] == 2
    assert usage_after_refund.json()["usage"]["interactions"] == 40

    default_rate_limiter.reset()

    # 22. Malformed / oversized requests
    oversized_resp = prod_client.post(
        "/v1/simulations",
        headers=api_auth_headers,
        json={
            "preset": "education",
            "num_interactions": 5000,  # Exceeds Free plan ceiling of 1000
        },
    )
    assert oversized_resp.status_code == 400
    assert oversized_resp.json()["error"]["details"]["code"] == "interaction_limit_exceeded"

    # 23. Structured error behavior
    invalid_preset_resp = prod_client.post(
        "/v1/simulations",
        headers=api_auth_headers,
        json={
            "preset": "non_existent_preset",
            "num_interactions": 10,
        },
    )
    assert invalid_preset_resp.status_code == 400
    err = invalid_preset_resp.json()["error"]
    assert "message" in err
    assert "status_code" in err
    assert "request_id" in err
    assert err["details"]["code"] == "unsupported_preset"

    # 15. API-key revocation
    del_resp = prod_client.delete(f"/v1/api-keys/{api_key_id}", headers=sess_auth_headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "revoked"

    # 16. Revoked-key rejection
    revoked_call = prod_client.get("/v1/account", headers=api_auth_headers)
    assert revoked_call.status_code == 401

    # 18. Logout (/v1/auth/logout)
    logout_resp = prod_client.post("/v1/auth/logout", headers=sess_auth_headers)
    assert logout_resp.status_code == 200
    assert logout_resp.json()["status"] == "ok"

    # 19. Post-logout rejection
    post_logout_call = prod_client.get("/v1/account", headers=sess_auth_headers)
    assert post_logout_call.status_code == 401

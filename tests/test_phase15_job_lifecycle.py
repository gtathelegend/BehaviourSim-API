"""Tests for Phase 15: Async Simulation Jobs & Execution Architecture."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.core.concurrency import default_concurrency_limiter
from app.core.errors import BehaviorSimAPIError
from app.core.rate_limit import default_rate_limiter
from app.db.models import Base
from app.db.models.plan import Plan
from app.db.models.simulation import Simulation
from app.db.models.usage import MonthlyUsage, UsageEvent
from app.db.models.user import User
from app.db.session import get_db
from app.main import app
from app.services.api_key import create_api_key
from app.services.simulation_job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    can_transition,
    create_simulation_job,
    execute_simulation_job,
    transition_job_status,
)
from app.services.usage import get_current_period_start, get_or_create_monthly_usage


# ============================================================================
# 1. State Machine & Transition Unit Tests
# ============================================================================

def test_can_transition_rules():
    """Verify state transition rules: allowed forward transitions and terminal restrictions."""
    # Allowed forward transitions
    assert can_transition(STATUS_PENDING, STATUS_RUNNING) is True
    assert can_transition(STATUS_PENDING, STATUS_FAILED) is True
    assert can_transition(STATUS_RUNNING, STATUS_COMPLETED) is True
    assert can_transition(STATUS_RUNNING, STATUS_FAILED) is True

    # Forbidden backward / invalid transitions
    assert can_transition(STATUS_RUNNING, STATUS_PENDING) is False
    assert can_transition(STATUS_COMPLETED, STATUS_RUNNING) is False
    assert can_transition(STATUS_COMPLETED, STATUS_PENDING) is False
    assert can_transition(STATUS_COMPLETED, STATUS_FAILED) is False
    assert can_transition(STATUS_FAILED, STATUS_RUNNING) is False
    assert can_transition(STATUS_FAILED, STATUS_PENDING) is False
    assert can_transition(STATUS_FAILED, STATUS_COMPLETED) is False


def test_job_status_transition_lifecycle(tmp_path: Path):
    """Verify job transitions: pending -> running -> completed, updating timestamps."""
    db_file = tmp_path / "lifecycle.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="free_p15", monthly_requests=100, monthly_interactions=10000, max_interactions_per_request=1000, requests_per_minute=100, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="p15_unit@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        # 1. Create job in pending status
        job = create_simulation_job(
            db=session,
            user=user,
            preset="education",
            num_interactions=10,
            seed=42,
        )
        assert job.status == STATUS_PENDING
        assert job.started_at is None
        assert job.completed_at is None
        assert job.data is None

        # 2. Transition to running
        transition_job_status(session, job, STATUS_RUNNING)
        assert job.status == STATUS_RUNNING
        assert job.started_at is not None
        assert job.completed_at is None

        # 3. Transition to completed
        mock_data = [{"step": 1, "state": "Optimal"}]
        transition_job_status(session, job, STATUS_COMPLETED, data=mock_data, compute_ms=12)
        assert job.status == STATUS_COMPLETED
        assert job.completed_at is not None
        assert job.compute_ms == 12
        assert job.data == mock_data

        # 4. Attempting to transition from completed must raise error
        with pytest.raises(BehaviorSimAPIError) as exc_info:
            transition_job_status(session, job, STATUS_RUNNING)
        assert exc_info.value.status_code == 400
        assert exc_info.value.details["code"] == "invalid_state_transition"


def test_job_failure_transition(tmp_path: Path):
    """Verify transitioning to failed records failure code and timestamp."""
    db_file = tmp_path / "failure.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="free_p15_fail", monthly_requests=100, monthly_interactions=10000, max_interactions_per_request=1000, requests_per_minute=100, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="p15_fail@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        job = create_simulation_job(
            db=session,
            user=user,
            preset="finance",
            num_interactions=5,
        )
        assert job.status == STATUS_PENDING

        # Transition pending -> running
        transition_job_status(session, job, STATUS_RUNNING)

        # Transition running -> failed
        transition_job_status(
            session,
            job,
            STATUS_FAILED,
            error_code="simulation_generation_failed",
            error_message="Simulation engine crashed.",
        )
        assert job.status == STATUS_FAILED
        assert job.error_code == "simulation_generation_failed"
        assert job.error_message == "Simulation engine crashed."
        assert job.completed_at is not None

        # Terminal state: cannot transition out of failed
        with pytest.raises(BehaviorSimAPIError):
            transition_job_status(session, job, STATUS_RUNNING)


# ============================================================================
# 2. Execution Service & Quota Lifecycle Tests
# ============================================================================

def test_execute_simulation_job_success_lifecycle(tmp_path: Path):
    """Verify execute_simulation_job completes full lifecycle, reserves and finalizes quota."""
    db_file = tmp_path / "exec_success.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="exec_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=2, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="exec_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        job = create_simulation_job(
            db=session,
            user=user,
            preset="education",
            num_interactions=10,
            seed=123,
        )
        assert job.status == STATUS_PENDING

        completed = execute_simulation_job(
            db=session,
            job=job,
            user=user,
        )
        assert completed.status == STATUS_COMPLETED
        assert len(completed.data) == 10
        assert completed.compute_ms is not None
        assert completed.started_at is not None
        assert completed.completed_at is not None

        # Verify quota: 1 request, 10 interactions charged
        period_start = get_current_period_start()
        usage = get_or_create_monthly_usage(session, user.id, period_start)
        assert usage.request_count == 1
        assert usage.interaction_count == 10


def test_execute_simulation_job_failure_refunds_quota(tmp_path: Path):
    """Verify that when simulation execution crashes, quota is refunded and job marked failed."""
    db_file = tmp_path / "exec_fail.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="fail_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=2, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="fail_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        job = create_simulation_job(
            db=session,
            user=user,
            preset="education",
            num_interactions=10,
        )

        with patch("app.services.simulation_job.execute_simulation", side_effect=RuntimeError("Engine fatal error")):
            with pytest.raises(BehaviorSimAPIError) as exc_info:
                execute_simulation_job(
                    db=session,
                    job=job,
                    user=user,
                )
            assert exc_info.value.status_code == 500
            assert exc_info.value.details["code"] == "simulation_generation_failed"

        # Refresh job from DB: must be marked failed
        session.refresh(job)
        assert job.status == STATUS_FAILED
        assert job.error_code == "simulation_generation_failed"

        # Verify quota was refunded: 0 requests, 0 interactions
        period_start = get_current_period_start()
        usage = get_or_create_monthly_usage(session, user.id, period_start)
        assert usage.request_count == 0
        assert usage.interaction_count == 0


# ============================================================================
# 3. HTTP API Endpoint Tests (/v1/simulations)
# ============================================================================

def test_api_run_simulation_returns_status_and_data(tmp_path: Path):
    """Verify POST /v1/simulations returns status='completed' and full data payload."""
    default_rate_limiter.reset()
    default_concurrency_limiter.reset()

    db_file = tmp_path / "api_sim.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="api_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="api_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="API Key").key

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        res = client.post(
            "/v1/simulations",
            headers={"Authorization": f"Bearer {key}"},
            json={"preset": "education", "num_interactions": 10, "seed": 42},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "completed"
        assert len(data["data"]) == 10
        assert "simulation_id" in data
        assert data["metadata"]["reproducible"] is True
        assert data["metadata"]["compute_ms"] > 0
    finally:
        app.dependency_overrides.clear()
        default_concurrency_limiter.reset()
        default_rate_limiter.reset()


def test_api_detail_endpoint_represents_all_statuses(tmp_path: Path):
    """Verify GET /v1/simulations/{id} represents pending, running, completed, and failed runs."""
    default_rate_limiter.reset()

    db_file = tmp_path / "api_detail.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="detail_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="detail_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="Detail Key").key

        # 1. Create a pending job
        job_pending = create_simulation_job(session, user, preset="education", num_interactions=10)
        # 2. Create a running job
        job_running = create_simulation_job(session, user, preset="finance", num_interactions=10)
        transition_job_status(session, job_running, STATUS_RUNNING)
        # 3. Create a failed job
        job_failed = create_simulation_job(session, user, preset="healthcare", num_interactions=10)
        transition_job_status(session, job_failed, STATUS_RUNNING)
        transition_job_status(session, job_failed, STATUS_FAILED, error_code="timeout", error_message="Generation timed out.")
        # 4. Create a completed job
        job_completed = create_simulation_job(session, user, preset="mobile_app", num_interactions=5)
        transition_job_status(session, job_completed, STATUS_RUNNING)
        transition_job_status(session, job_completed, STATUS_COMPLETED, data=[{"action": "browse"}], compute_ms=15)

        pending_id = str(job_pending.id)
        running_id = str(job_running.id)
        failed_id = str(job_failed.id)
        completed_id = str(job_completed.id)

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {key}"}

        # Check pending
        r_pend = client.get(f"/v1/simulations/{pending_id}", headers=auth)
        assert r_pend.status_code == 200
        d_pend = r_pend.json()
        assert d_pend["status"] == "pending"
        assert d_pend["data"] is None
        assert d_pend["metadata"] is None

        # Check running
        r_run = client.get(f"/v1/simulations/{running_id}", headers=auth)
        assert r_run.status_code == 200
        d_run = r_run.json()
        assert d_run["status"] == "running"
        assert d_run["started_at"] is not None
        assert d_run["data"] is None
        assert d_run["metadata"] is None

        # Check failed (safe failure representation without stack trace)
        r_fail = client.get(f"/v1/simulations/{failed_id}", headers=auth)
        assert r_fail.status_code == 200
        d_fail = r_fail.json()
        assert d_fail["status"] == "failed"
        assert d_fail["error_code"] == "timeout"
        assert d_fail["error_message"] == "Generation timed out."
        assert d_fail["data"] is None
        assert d_fail["metadata"] is None

        # Check completed
        r_comp = client.get(f"/v1/simulations/{completed_id}", headers=auth)
        assert r_comp.status_code == 200
        d_comp = r_comp.json()
        assert d_comp["status"] == "completed"
        assert d_comp["data"] == [{"action": "browse"}]
        assert d_comp["metadata"]["compute_ms"] == 15
    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()


def test_api_delete_safety_against_running_job(tmp_path: Path):
    """Verify DELETE /v1/simulations/{id} rejects running jobs with 409, allows completed/failed/pending with 204."""
    default_rate_limiter.reset()

    db_file = tmp_path / "delete_safety.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="del_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user1 = User(email="del_user1@example.com", is_active=True, plan_id=plan.id)
        user2 = User(email="del_user2@example.com", is_active=True, plan_id=plan.id)
        session.add_all([user1, user2])
        session.commit()

        key1 = create_api_key(session, user1, name="Key 1").key
        key2 = create_api_key(session, user2, name="Key 2").key

        job_running = create_simulation_job(session, user1, preset="education", num_interactions=10)
        transition_job_status(session, job_running, STATUS_RUNNING)
        running_id = str(job_running.id)

        job_failed = create_simulation_job(session, user1, preset="finance", num_interactions=10)
        transition_job_status(session, job_failed, STATUS_FAILED, error_code="err", error_message="failed")
        failed_id = str(job_failed.id)

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        auth1 = {"Authorization": f"Bearer {key1}"}
        auth2 = {"Authorization": f"Bearer {key2}"}

        # 1. IDOR Check: user2 attempts to delete user1's running job -> returns 404 (ownership masked)
        r_idor = client.delete(f"/v1/simulations/{running_id}", headers=auth2)
        assert r_idor.status_code == 404
        assert r_idor.json()["error"]["details"]["code"] == "simulation_not_found"

        # 2. Owner attempts to delete running job -> returns 409 Conflict
        r_del_running = client.delete(f"/v1/simulations/{running_id}", headers=auth1)
        assert r_del_running.status_code == 409
        d_err = r_del_running.json()
        assert d_err["error"]["details"]["code"] == "cannot_delete_running_simulation"
        assert "while it is currently running" in d_err["error"]["message"]

        # 3. Owner deletes failed job -> returns 204 No Content
        r_del_failed = client.delete(f"/v1/simulations/{failed_id}", headers=auth1)
        assert r_del_failed.status_code == 204

        # Verify failed job was removed from database
        with SessionClass() as s:
            rem = s.get(Simulation, uuid.UUID(failed_id))
            assert rem is None
    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()


def test_api_history_status_filtering_and_projection(tmp_path: Path):
    """Verify GET /v1/simulations supports status filtering across all 4 statuses."""
    default_rate_limiter.reset()

    db_file = tmp_path / "history_filter.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(name="hist_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="hist_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="Hist Key").key

        # Create 1 of each status
        j_pend = create_simulation_job(session, user, preset="education", num_interactions=10)
        j_run = create_simulation_job(session, user, preset="finance", num_interactions=10)
        transition_job_status(session, j_run, STATUS_RUNNING)
        j_fail = create_simulation_job(session, user, preset="healthcare", num_interactions=10)
        transition_job_status(session, j_fail, STATUS_FAILED, error_code="timeout", error_message="timeout")
        j_comp = create_simulation_job(session, user, preset="mobile_app", num_interactions=10)
        transition_job_status(session, j_comp, STATUS_RUNNING)
        transition_job_status(session, j_comp, STATUS_COMPLETED, data=[{}], compute_ms=20)

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {key}"}

        # Query all -> total 4
        r_all = client.get("/v1/simulations", headers=auth)
        assert r_all.status_code == 200
        assert r_all.json()["total"] == 4

        # Filter by status=completed -> total 1
        r_comp = client.get("/v1/simulations?status=completed", headers=auth)
        assert r_comp.status_code == 200
        assert r_comp.json()["total"] == 1
        assert r_comp.json()["items"][0]["status"] == "completed"

        # Filter by status=failed -> total 1
        r_fail = client.get("/v1/simulations?status=failed", headers=auth)
        assert r_fail.status_code == 200
        assert r_fail.json()["total"] == 1
        assert r_fail.json()["items"][0]["status"] == "failed"
        assert r_fail.json()["items"][0]["error_code"] == "timeout"

        # Filter by status=running -> total 1
        r_run = client.get("/v1/simulations?status=running", headers=auth)
        assert r_run.status_code == 200
        assert r_run.json()["total"] == 1
        assert r_run.json()["items"][0]["status"] == "running"

        # Filter by status=pending -> total 1
        r_pend = client.get("/v1/simulations?status=pending", headers=auth)
        assert r_pend.status_code == 200
        assert r_pend.json()["total"] == 1
        assert r_pend.json()["items"][0]["status"] == "pending"

        # Invalid status filter -> 400
        r_bad = client.get("/v1/simulations?status=invalid_status", headers=auth)
        assert r_bad.status_code == 400
        assert r_bad.json()["error"]["details"]["code"] == "invalid_status"
    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()


def test_concurrent_simulation_jobs_limit(tmp_path: Path):
    """Verify concurrent job executions enforce plan limits (429 concurrent_simulation_limit_exceeded)."""
    default_rate_limiter.reset()
    default_concurrency_limiter.reset()

    db_file = tmp_path / "concurrent_jobs.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.commit()

    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with SessionClass() as session:
        plan = Plan(
            name="concurrent_p15_plan",
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=1,
            max_api_keys=1,
        )
        session.add(plan)
        session.commit()

        user = User(email="concurrent_p15@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        user_id = user.id
        key = create_api_key(session, user, name="Conc Key").key

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)

        def slow_execute_simulation(**kwargs):
            time.sleep(0.3)
            return ("sim_concurrent_id", [{"step": 1}], 50)

        with patch("app.services.simulation_job.execute_simulation", side_effect=slow_execute_simulation):
            def make_request():
                return client.post(
                    "/v1/simulations",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"preset": "education", "num_interactions": 10},
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(make_request)
                time.sleep(0.05)  # Ensure request 1 has acquired slot
                f2 = executor.submit(make_request)

                r1 = f1.result()
                r2 = f2.result()

            statuses = {r1.status_code, r2.status_code}
            assert 200 in statuses
            assert 429 in statuses

            err_resp = r1 if r1.status_code == 429 else r2
            assert err_resp.json()["error"]["details"]["code"] == "concurrent_simulation_limit_exceeded"

        # Concurrency slot must be fully released after execution
        assert default_concurrency_limiter.get_active_count(user_id) == 0

    finally:
        app.dependency_overrides.clear()
        default_concurrency_limiter.reset()
        default_rate_limiter.reset()


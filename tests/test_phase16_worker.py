"""Tests for Phase 16: Distributed Job Execution & Worker Architecture."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
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
    create_simulation_job,
    utc_now,
)
from app.services.usage import get_current_period_start, get_or_create_monthly_usage
from app.services.worker import claim_next_job, process_claimed_job
from app.worker import SimulationWorker


# ============================================================================
# Helper fixture for fresh isolated SQLite DB
# ============================================================================

def setup_test_sqlite(tmp_path: Path, name: str):
    db_file = tmp_path / f"{name}.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    SessionClass = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return SessionClass


# ============================================================================
# 1. API POST Behavior: Async 202 vs Sync 200
# ============================================================================

def test_post_creates_pending_job_202(tmp_path: Path):
    """Verify POST /v1/simulations defaults to returning 202 Accepted with status='pending'."""
    default_rate_limiter.reset()
    SessionClass = setup_test_sqlite(tmp_path, "api_async_202")

    with SessionClass() as session:
        plan = Plan(name="plan_202", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="user_202@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="Key 202").key
        user_id = user.id

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        res = client.post(
            "/v1/simulations",
            headers={"Authorization": f"Bearer {key}"},
            json={"preset": "education", "num_interactions": 20, "seed": 42},
        )
        assert res.status_code == 202
        data = res.json()
        assert data["status"] == "pending"
        assert "simulation_id" in data
        assert data["num_interactions"] == 20
        assert data["preset"] == "education"
        assert "created_at" in data

        # Check DB row is pending and quota is reserved
        with SessionClass() as s:
            sim = s.get(Simulation, uuid.UUID(data["simulation_id"]))
            assert sim is not None
            assert sim.status == STATUS_PENDING
            assert sim.data is None
            assert sim.compute_ms is None

            usage = get_or_create_monthly_usage(s, user_id, get_current_period_start())
            assert usage.request_count == 1
            assert usage.interaction_count == 20
    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()


def test_transitional_sync_mode(tmp_path: Path):
    """Verify POST /v1/simulations?sync=true executes synchronously and returns 200 OK with data."""
    default_rate_limiter.reset()
    default_concurrency_limiter.reset()
    SessionClass = setup_test_sqlite(tmp_path, "api_sync_200")

    with SessionClass() as session:
        plan = Plan(name="plan_sync", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="user_sync@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="Key Sync").key

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        res = client.post(
            "/v1/simulations?sync=true",
            headers={"Authorization": f"Bearer {key}"},
            json={"preset": "education", "num_interactions": 10, "seed": 42},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "completed"
        assert len(data["data"]) == 10
        assert data["metadata"]["compute_ms"] > 0
    finally:
        app.dependency_overrides.clear()
        default_concurrency_limiter.reset()
        default_rate_limiter.reset()


# ============================================================================
# 2. Worker Claim & Execution Mechanics
# ============================================================================

def test_worker_claims_and_completes_job(tmp_path: Path):
    """Verify worker claims pending job, transitions to running, then completed with results."""
    SessionClass = setup_test_sqlite(tmp_path, "worker_claim_exec")

    with SessionClass() as session:
        plan = Plan(name="worker_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="worker_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        # Create pending job
        job = create_simulation_job(
            db=session,
            user=user,
            preset="education",
            num_interactions=10,
            seed=99,
        )
        job_id = job.id

    worker = SimulationWorker(worker_id="test-worker-1", session_factory=SessionClass)

    # Run one iteration of worker
    did_work = worker.run_once()
    assert did_work is True

    # Verify job in DB
    with SessionClass() as session:
        updated_job = session.get(Simulation, job_id)
        assert updated_job.status == STATUS_COMPLETED
        assert updated_job.worker_id == "test-worker-1"
        assert updated_job.attempt_count == 1
        assert len(updated_job.data) == 10
        assert updated_job.compute_ms is not None
        assert updated_job.completed_at is not None
        assert updated_job.claimed_at is not None


def test_only_one_worker_can_claim_job(tmp_path: Path):
    """Verify multiple workers attempting to claim the same single job results in exactly one claim."""
    SessionClass = setup_test_sqlite(tmp_path, "worker_single_claim")

    with SessionClass() as session:
        plan = Plan(name="single_claim_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="single_claim@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        job = create_simulation_job(session, user, preset="education", num_interactions=10)
        job_id = job.id

    with SessionClass() as s1, SessionClass() as s2:
        claimed_1 = claim_next_job(s1, worker_id="worker-A")
        claimed_2 = claim_next_job(s2, worker_id="worker-B")

    # One worker gets the job, the second gets None
    claims = [claimed_1, claimed_2]
    successful_claims = [c for c in claims if c is not None]
    assert len(successful_claims) == 1
    assert successful_claims[0].id == job_id


def test_completed_or_failed_job_cannot_be_claimed(tmp_path: Path):
    """Verify completed or failed jobs are never returned by claim_next_job."""
    SessionClass = setup_test_sqlite(tmp_path, "no_reclaim_terminal")

    with SessionClass() as session:
        plan = Plan(name="term_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="term_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        # Completed job
        j_comp = create_simulation_job(session, user, preset="education", num_interactions=10)
        j_comp.status = STATUS_COMPLETED
        # Failed job
        j_fail = create_simulation_job(session, user, preset="finance", num_interactions=10)
        j_fail.status = STATUS_FAILED
        session.commit()

        claimed = claim_next_job(session, worker_id="worker-term")
        assert claimed is None


# ============================================================================
# 3. Failure & Quota Recovery Mechanics
# ============================================================================

def test_worker_survives_job_failure_and_refunds_quota(tmp_path: Path):
    """Verify that when simulation engine crashes, job is marked failed, quota refunded, and worker survives."""
    SessionClass = setup_test_sqlite(tmp_path, "worker_failure_recovery")

    with SessionClass() as session:
        plan = Plan(name="fail_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="fail_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        user_id = user.id

        # Simulate quota reservation for 25 interactions
        period_start = get_current_period_start()
        usage = get_or_create_monthly_usage(session, user.id, period_start)
        usage.request_count = 1
        usage.interaction_count = 25
        session.commit()

        job = create_simulation_job(session, user, preset="education", num_interactions=25)
        job_id = job.id

    worker = SimulationWorker(worker_id="fail-worker", session_factory=SessionClass)

    with patch("app.services.worker._get_execute_simulation", side_effect=lambda: (_ for _ in ()).throw(RuntimeError("Engine fatal boom"))):
        did_work = worker.run_once()
        assert did_work is True

    with SessionClass() as session:
        j = session.get(Simulation, job_id)
        assert j.status == STATUS_FAILED
        assert j.error_code == "simulation_generation_failed"
        assert j.completed_at is not None

        # Quota refunded
        u = get_or_create_monthly_usage(session, user_id, period_start)
        assert u.request_count == 0
        assert u.interaction_count == 0

        # Usage event recorded
        ev = session.query(UsageEvent).filter_by(user_id=user_id, event_type="simulation_failed").first()
        assert ev is not None
        assert ev.success is False


def test_stuck_job_recovery_and_max_attempts_exhaustion(tmp_path: Path):
    """Verify abandoned running job is recovered when lease expires, and failed if max attempts reached."""
    SessionClass = setup_test_sqlite(tmp_path, "stuck_job_test")

    with SessionClass() as session:
        plan = Plan(name="stuck_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="stuck_user@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        user_id = user.id

        period_start = get_current_period_start()
        usage = get_or_create_monthly_usage(session, user.id, period_start)
        usage.request_count = 1
        usage.interaction_count = 10
        session.commit()

        # Create a stuck running job with expired heartbeat and attempt_count = 1
        stuck_job = create_simulation_job(session, user, preset="education", num_interactions=10)
        stuck_job.status = STATUS_RUNNING
        stuck_job.worker_id = "dead-worker-1"
        stuck_job.attempt_count = 1
        stuck_job.max_attempts = 2
        stuck_job.heartbeat_at = utc_now() - timedelta(seconds=600)  # 10 mins ago (lease expired)
        session.commit()
        job_id = stuck_job.id

        # 1. First recovery attempt (attempt 2 of 2)
        recovered = claim_next_job(session, worker_id="recovery-worker", lease_timeout_seconds=300)
        assert recovered is not None
        assert recovered.id == job_id
        assert recovered.worker_id == "recovery-worker"
        assert recovered.attempt_count == 2
        assert recovered.status == STATUS_RUNNING

        # Simulate worker dying again: expire heartbeat again
        recovered.heartbeat_at = utc_now() - timedelta(seconds=600)
        session.commit()

        # 2. Next claim attempt: attempt_count (2) >= max_attempts (2) -> must transition to terminal FAILED
        no_more = claim_next_job(session, worker_id="another-worker", lease_timeout_seconds=300)
        assert no_more is None

        # Verify job is now marked failed with execution_timeout
        final_job = session.get(Simulation, job_id)
        assert final_job.status == STATUS_FAILED
        assert final_job.error_code == "execution_timeout"

        # Quota must be refunded
        u = get_or_create_monthly_usage(session, user_id, period_start)
        assert u.request_count == 0
        assert u.interaction_count == 0


# ============================================================================
# 4. Concurrency Enforcement Across Workers
# ============================================================================

def test_database_backed_concurrency_limit_across_workers(tmp_path: Path):
    """Verify that a Free plan user with max_concurrent_simulations=1 cannot have 2 jobs claimed concurrently."""
    SessionClass = setup_test_sqlite(tmp_path, "concurrency_limit_test")

    with SessionClass() as session:
        plan = Plan(name="single_sim_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="single_sim@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()

        # Create two pending jobs for the same user
        j1 = create_simulation_job(session, user, preset="education", num_interactions=5)
        j2 = create_simulation_job(session, user, preset="finance", num_interactions=5)
        session.commit()
        j1_id = j1.id
        j2_id = j2.id

    # Worker 1 claims job 1
    with SessionClass() as s1:
        c1 = claim_next_job(s1, worker_id="worker-1")
        assert c1 is not None
        assert c1.id == j1_id
        assert c1.status == STATUS_RUNNING

    # Worker 2 attempts to claim job 2 while job 1 is running for the same user
    with SessionClass() as s2:
        c2 = claim_next_job(s2, worker_id="worker-2")
        # Must be rejected / skipped because user already has 1 running simulation!
        assert c2 is None

    # Now simulate Worker 1 finishing job 1
    with SessionClass() as s1:
        j1_row = s1.get(Simulation, j1_id)
        j1_row.status = STATUS_COMPLETED
        s1.commit()

    # Now Worker 2 is able to claim job 2
    with SessionClass() as s2:
        c2 = claim_next_job(s2, worker_id="worker-2")
        assert c2 is not None
        assert c2.id == j2_id
        assert c2.status == STATUS_RUNNING


def test_concurrent_different_users_can_execute_simultaneously(tmp_path: Path):
    """Verify that two different users can have jobs claimed simultaneously by separate workers."""
    SessionClass = setup_test_sqlite(tmp_path, "different_users_concurrency")

    with SessionClass() as session:
        plan = Plan(name="diff_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        u1 = User(email="u1@example.com", is_active=True, plan_id=plan.id)
        u2 = User(email="u2@example.com", is_active=True, plan_id=plan.id)
        session.add_all([u1, u2])
        session.commit()

        j1 = create_simulation_job(session, u1, preset="education", num_interactions=5)
        j2 = create_simulation_job(session, u2, preset="finance", num_interactions=5)
        session.commit()

    with SessionClass() as s1, SessionClass() as s2:
        c1 = claim_next_job(s1, worker_id="worker-1")
        c2 = claim_next_job(s2, worker_id="worker-2")

        assert c1 is not None
        assert c2 is not None
        assert c1.user_id != c2.user_id
        assert {c1.status, c2.status} == {STATUS_RUNNING}


# ============================================================================
# 5. Deletion Races & IDOR Protection
# ============================================================================

def test_delete_pending_job_before_worker_claim_refunds_quota(tmp_path: Path):
    """Verify deleting a pending job permanently removes it, refunds quota, and worker never sees it."""
    default_rate_limiter.reset()
    SessionClass = setup_test_sqlite(tmp_path, "delete_pending_refund")

    with SessionClass() as session:
        plan = Plan(name="del_pend_plan", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="del_pend@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="Del Key").key
        user_id = user.id

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {key}"}

        # 1. Post job -> 202 Accepted
        res_post = client.post("/v1/simulations", headers=auth, json={"preset": "education", "num_interactions": 30})
        assert res_post.status_code == 202
        sim_id = res_post.json()["simulation_id"]

        # 2. Check quota was reserved
        with SessionClass() as s:
            u = get_or_create_monthly_usage(s, user_id, get_current_period_start())
            assert u.request_count == 1
            assert u.interaction_count == 30

        # 3. Delete pending job -> 204 No Content
        res_del = client.delete(f"/v1/simulations/{sim_id}", headers=auth)
        assert res_del.status_code == 204

        # 4. Quota must be refunded!
        with SessionClass() as s:
            u = get_or_create_monthly_usage(s, user_id, get_current_period_start())
            assert u.request_count == 0
            assert u.interaction_count == 0

            # Job is gone from DB
            assert s.get(Simulation, uuid.UUID(sim_id)) is None

        # 5. Worker polling must find 0 jobs
        worker = SimulationWorker(worker_id="w-none", session_factory=SessionClass)
        assert worker.run_once() is False

    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()


def test_delete_running_job_rejected_409(tmp_path: Path):
    """Verify deleting a currently running job is rejected with 409 Conflict."""
    default_rate_limiter.reset()
    SessionClass = setup_test_sqlite(tmp_path, "del_running_409")

    with SessionClass() as session:
        plan = Plan(name="p_409", monthly_requests=10, monthly_interactions=1000, max_interactions_per_request=100, requests_per_minute=50, max_concurrent_simulations=1, max_api_keys=1)
        session.add(plan)
        session.commit()

        user = User(email="u_409@example.com", is_active=True, plan_id=plan.id)
        session.add(user)
        session.commit()
        key = create_api_key(session, user, name="Key 409").key

        job = create_simulation_job(session, user, preset="education", num_interactions=10)
        job.status = STATUS_RUNNING
        session.commit()
        job_id = str(job.id)

    def override_db():
        with SessionClass() as s:
            yield s

    app.dependency_overrides[get_db] = override_db

    try:
        client = TestClient(app)
        auth = {"Authorization": f"Bearer {key}"}

        res = client.delete(f"/v1/simulations/{job_id}", headers=auth)
        assert res.status_code == 409
        assert res.json()["error"]["details"]["code"] == "cannot_delete_running_simulation"
    finally:
        app.dependency_overrides.clear()
        default_rate_limiter.reset()

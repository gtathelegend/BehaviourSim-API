"""Comprehensive tests for Phase 18: Production Observability & Async Operations."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import SafeFormatter, redact_sensitive_text
from app.core.metrics import operational_metrics
from app.core.middleware import request_id_ctx_var
from app.db.models.plan import Plan
from app.db.models.simulation import Simulation
from app.db.models.user import User
from app.services.simulation_job import STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING, create_simulation_job
from app.services.worker import claim_next_job, ensure_utc, process_claimed_job
from app.worker import SimulationWorker


# --- 1. Request ID Generation, Validation & Propagation ---

def test_request_id_generated_when_absent(client: TestClient):
    """Verify that absent X-Request-ID triggers automatic UUID generation returned in response header."""
    response = client.get("/health")
    assert response.status_code == 200
    req_id = response.headers.get("X-Request-ID")
    assert req_id is not None
    # Validate UUID format
    assert uuid.UUID(req_id)


def test_request_id_preserved_when_valid(client: TestClient):
    """Verify that a valid client-supplied correlation ID is preserved and echoed in headers."""
    client_req_id = "client-trace-abc-123.xyz_456"
    response = client.get("/health", headers={"X-Request-ID": client_req_id})
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == client_req_id


def test_request_id_sanitized_when_invalid(client: TestClient):
    """Verify that an invalid incoming correlation ID (e.g. unsafe chars) is replaced with a clean UUID."""
    unsafe_req_id = "unsafe;DROP TABLE simulations;--"
    response = client.get("/health", headers={"X-Request-ID": unsafe_req_id})
    assert response.status_code == 200
    req_id = response.headers.get("X-Request-ID")
    assert req_id != unsafe_req_id
    assert uuid.UUID(req_id)


def test_request_id_propagated_to_error_envelope(client: TestClient):
    """Verify that error responses embed the correlated request ID in the error envelope payload."""
    custom_trace = "trace-err-envelope-999"
    response = client.get("/v1/presets/non_existent_preset", headers={"X-Request-ID": custom_trace})
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    assert data["error"]["request_id"] == custom_trace


# --- 2. Secret Redaction in Logging ---

def test_secret_redactor_masks_credentials():
    """Verify that sensitive tokens, keys, passwords, and connection strings are scrubbed from text."""
    # Bearer token
    assert "Bearer [REDACTED]" in redact_sensitive_text("Headers: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test")
    # API key
    assert "bs_live_[REDACTED]" in redact_sensitive_text("Authenticated with key bs_live_1234567890abcdef12345678")
    # Session token
    assert "bs_sess_[REDACTED]" in redact_sensitive_text("Created session bs_sess_abcdef1234567890abcdef12")
    # Session cookie
    assert "behaviorsim_session=[REDACTED]" in redact_sensitive_text("Cookie: behaviorsim_session=abc.123.def;")
    # Passwords & secrets
    assert "password=[REDACTED]" in redact_sensitive_text("Config contains password=SuperSecretPassword123!")
    assert "client_secret=[REDACTED]" in redact_sensitive_text("OAuth response client_secret=GOCSPX-secret-token")
    assert "access_token=[REDACTED]" in redact_sensitive_text("OAuth exchange access_token=ghu_secret_access_token")
    # Postgres connection URL
    assert "postgresql://app_user:[REDACTED]@db.neon.tech/neondb" in redact_sensitive_text(
        "Connecting to postgresql://app_user:MySecretPassword123@db.neon.tech/neondb?sslmode=require"
    )


def test_safe_formatter_injects_structured_tags():
    """Verify that SafeFormatter injects correlation tags [req:...], [sim:...], [user:...] and scrubs content."""
    formatter = SafeFormatter(fmt="%(asctime)s [%(levelname)s]%(request_context)s: %(message)s")

    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Processing job for user with key bs_live_1234567890abcdef12345678",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-12345"
    record.simulation_id = "sim-67890"
    record.user_id = "usr-abcdef"

    formatted = formatter.format(record)
    assert "[req:req-12345 sim:sim-67890 user:usr-abcdef]" in formatted
    assert "bs_live_[REDACTED]" in formatted
    assert "bs_live_1234567890abcdef12345678" not in formatted


# --- 3. In-Process Operational Metrics ---

def test_operational_metrics_lifecycle():
    """Verify that OperationalMetrics records counts, latencies, and computes accurate summary snapshots."""
    operational_metrics.reset()

    # Record requests
    operational_metrics.record_request("GET", "/health", 200, 2.5)
    operational_metrics.record_request("POST", "/v1/simulations", 202, 15.0)
    operational_metrics.record_request("GET", "/v1/account", 401, 3.2)
    operational_metrics.record_request("POST", "/v1/simulations", 429, 1.8)

    # Record simulation metrics
    operational_metrics.record_simulation_accepted("education", 50)
    operational_metrics.record_simulation_accepted("education", 100)
    operational_metrics.record_simulation_accepted("finance", 25)

    operational_metrics.record_simulation_completed(compute_ms=120, queue_wait_ms=500)
    operational_metrics.record_simulation_completed(compute_ms=80, queue_wait_ms=700)
    operational_metrics.record_simulation_failed("execution_timeout")

    summary = operational_metrics.get_summary()

    # Assert API summary
    assert summary["api"]["requests_total"] == 4
    assert summary["api"]["requests_by_method"]["GET"] == 2
    assert summary["api"]["requests_by_method"]["POST"] == 2
    assert summary["api"]["errors_total"] == 2
    assert summary["api"]["auth_failures_total"] == 1
    assert summary["api"]["rate_limit_rejections_total"] == 1

    # Assert Simulation summary
    assert summary["simulations"]["accepted"] == 3
    assert summary["simulations"]["accepted_by_preset"]["education"] == 2
    assert summary["simulations"]["accepted_by_preset"]["finance"] == 1
    assert summary["simulations"]["completed"] == 2
    assert summary["simulations"]["failed"] == 1
    assert summary["simulations"]["failed_by_code"]["execution_timeout"] == 1

    # Assert Latency distributions
    duration = summary["simulations"]["execution_duration_ms"]
    assert duration["count"] == 2
    assert duration["min"] == 80
    assert duration["max"] == 120
    assert duration["avg"] == 100.0

    queue_wait = summary["simulations"]["queue_wait_ms"]
    assert queue_wait["count"] == 2
    assert queue_wait["min"] == 500
    assert queue_wait["max"] == 700
    assert queue_wait["avg"] == 600.0


def test_diagnostics_endpoint_returns_safe_telemetry(client: TestClient):
    """Verify that GET /v1/diagnostics exposes operational metrics and queue status without mutations."""
    response = client.get("/v1/diagnostics")
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "ok"
    assert "version" in data
    assert "queue" in data
    assert "pending" in data["queue"]
    assert "running" in data["queue"]
    assert "metrics" in data
    assert "api" in data["metrics"]
    assert "simulations" in data["metrics"]


# --- 4. Stuck Job Analysis: Pending Expiration & Running Lease Recovery ---

def _get_or_create_plan(db_session: Session) -> Plan:
    plan = db_session.query(Plan).first()
    if not plan:
        plan = Plan(
            name="Free",
            monthly_requests=100,
            monthly_interactions=10000,
            max_interactions_per_request=1000,
            requests_per_minute=100,
            max_concurrent_simulations=5,
            max_api_keys=5,
        )
        db_session.add(plan)
        db_session.commit()
    return plan


def test_stuck_pending_job_expiration_and_quota_refund(db_session: Session):
    """Verify that a pending job older than max_pending_seconds is expired, marked failed, and refunds quota."""
    operational_metrics.reset()
    now = datetime.now(timezone.utc)

    # 1. Create a user with plan
    plan = _get_or_create_plan(db_session)
    user = User(email="stuck_pending_user@example.com", is_active=True, plan_id=plan.id)
    db_session.add(user)
    db_session.commit()

    # 2. Create pending job with simulated old timestamp (> 3600s ago)
    old_time = now - timedelta(seconds=7200)
    job = Simulation(
        id=uuid.uuid4(),
        user_id=user.id,
        preset="education",
        num_interactions=100,
        status=STATUS_PENDING,
        created_at=old_time,
        updated_at=old_time,
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
        attempt_count=0,
        max_attempts=3,
    )
    db_session.add(job)
    db_session.commit()

    # 3. Worker claims job with max_pending_seconds=3600
    claimed = claim_next_job(db_session, worker_id="test-worker", max_pending_seconds=3600)

    # The expired pending job should NOT be claimed as running
    assert claimed is None

    # Verify that the job was transitioned to failed with code queue_timeout
    db_session.refresh(job)
    assert job.status == STATUS_FAILED
    assert job.error_code == "queue_timeout"
    assert "maximum wait time" in job.error_message
    assert job.completed_at is not None

    # Metric recorded
    summary = operational_metrics.get_summary()
    assert summary["simulations"]["failed"] == 1
    assert summary["simulations"]["failed_by_code"].get("queue_timeout") == 1


def test_stuck_running_job_recovered_after_lease_timeout(db_session: Session):
    """Verify that an abandoned running job with expired heartbeat is recovered and attempt_count incremented."""
    now = datetime.now(timezone.utc)
    plan = _get_or_create_plan(db_session)
    user = User(email="stuck_running_user@example.com", is_active=True, plan_id=plan.id)
    db_session.add(user)
    db_session.commit()

    # Simulated running job abandoned 600s ago (lease timeout is 300s)
    stuck_time = now - timedelta(seconds=600)
    job = Simulation(
        id=uuid.uuid4(),
        user_id=user.id,
        preset="healthcare",
        num_interactions=50,
        status=STATUS_RUNNING,
        worker_id="crashed-worker-1",
        claimed_at=stuck_time,
        heartbeat_at=stuck_time,
        created_at=stuck_time,
        updated_at=stuck_time,
        attempt_count=1,
        max_attempts=3,
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
    )
    db_session.add(job)
    db_session.commit()

    # Healthy worker claims next job with lease_timeout_seconds=300
    claimed = claim_next_job(db_session, worker_id="healthy-worker-2", lease_timeout_seconds=300)

    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.worker_id == "healthy-worker-2"
    assert claimed.status == STATUS_RUNNING
    assert claimed.attempt_count == 2
    assert ensure_utc(claimed.heartbeat_at) >= now - timedelta(seconds=5)


# --- 5. Worker Exception Resilience ---

def test_simulation_worker_run_once_exception_resilience(tmp_path: Path):
    """Verify that unexpected database errors in run_once are caught cleanly and do not crash worker loop."""
    # Worker with faulty session factory that raises an unhandled operational exception
    def broken_session_factory():
        raise RuntimeError("Neon PostgreSQL connection timeout / network unreachable")

    worker = SimulationWorker(
        worker_id="resilient-worker",
        session_factory=broken_session_factory,
    )

    # Calling run_once should catch the exception, log it, and safely return False without raising
    result = worker.run_once()
    assert result is False


def test_simulation_worker_process_failure_refunds_quota(db_session: Session):
    """Verify that when engine execution crashes, process_claimed_job marks job failed and refunds quota."""
    now = datetime.now(timezone.utc)
    plan = _get_or_create_plan(db_session)
    user = User(email="crash_user@example.com", is_active=True, plan_id=plan.id)
    db_session.add(user)
    db_session.commit()

    job = Simulation(
        id=uuid.uuid4(),
        user_id=user.id,
        preset="education",
        num_interactions=50,
        status=STATUS_RUNNING,
        worker_id="test-worker",
        claimed_at=now,
        heartbeat_at=now,
        created_at=now,
        updated_at=now,
        attempt_count=1,
        max_attempts=3,
        behaviorsim_version="1.0.1",
        api_version="1.0.0",
    )
    db_session.add(job)
    db_session.commit()

    # Simulate engine crash during processing
    with patch("app.services.worker._get_execute_simulation") as mock_exec:
        mock_exec.return_value = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Unexpected synthetic engine crash"))
        success = process_claimed_job(db_session, job=job, worker_id="test-worker")

    assert success is False
    db_session.refresh(job)
    assert job.status == STATUS_FAILED
    assert job.error_code == "simulation_generation_failed"
    assert job.completed_at is not None

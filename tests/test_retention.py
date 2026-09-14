"""Tests for Phase 21: Simulation Data Lifecycle & Retention System."""

import concurrent.futures
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Generator
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.metrics import operational_metrics
from app.db.models.plan import Plan
from app.db.models.simulation import Simulation
from app.db.models.user import User
from app.services.retention import (
    CleanupResult,
    cleanup_expired_simulations,
    count_eligible_simulations,
    get_retention_cutoff,
    utc_now,
)
from app.services.simulation_job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    create_simulation_job,
)
from app.services.usage import reserve_usage
from tests.conftest import TestingSessionLocal


def create_test_user(db: Session, email_prefix: str = "retention_user") -> User:
    """Helper to create an active user with assigned free plan."""
    from app.services.plan import get_or_create_free_plan

    free_plan = get_or_create_free_plan(db)
    user = User(
        email=f"{email_prefix}_{uuid.uuid4().hex[:6]}@example.com",
        display_name="Retention Test User",
        plan_id=free_plan.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def create_historical_simulation(
    db: Session,
    user: User,
    status: str,
    age_days: float,
    num_interactions: int = 100,
    data: list = None,
) -> Simulation:
    """Helper to create a simulation record with an artificial historical created_at timestamp."""
    created_at = utc_now() - timedelta(days=age_days)
    sim = Simulation(
        id=uuid.uuid4(),
        user_id=user.id,
        preset="education",
        num_interactions=num_interactions,
        status=status,
        behaviorsim_version="1.0.1",
        api_version="v1",
        compute_ms=25 if status == STATUS_COMPLETED else None,
        data=data or ([{"t": i, "state": "Active"} for i in range(10)] if status == STATUS_COMPLETED else None),
        error_code="execution_timeout" if status == STATUS_FAILED else None,
        error_message="Simulation failed" if status == STATUS_FAILED else None,
        created_at=created_at,
        completed_at=created_at + timedelta(seconds=2) if status in (STATUS_COMPLETED, STATUS_FAILED) else None,
        updated_at=created_at,
    )
    db.add(sim)
    db.commit()
    db.refresh(sim)
    return sim


class TestRetentionEligibility:
    """Verify strict eligibility predicates and protection of active jobs."""

    def test_old_completed_is_deleted_recent_completed_is_retained(self, db_session: Session):
        user = create_test_user(db_session, "elig_completed")
        old_comp = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)
        recent_comp = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=3.0)

        result = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert result.success is True
        assert result.deleted_count == 1

        # Old is gone, recent remains
        assert db_session.get(Simulation, old_comp.id) is None
        assert db_session.get(Simulation, recent_comp.id) is not None

    def test_old_failed_is_deleted_recent_failed_is_retained(self, db_session: Session):
        user = create_test_user(db_session, "elig_failed")
        old_failed = create_historical_simulation(db_session, user, STATUS_FAILED, age_days=8.0)
        recent_failed = create_historical_simulation(db_session, user, STATUS_FAILED, age_days=2.0)

        result = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert result.success is True
        assert result.deleted_count == 1

        assert db_session.get(Simulation, old_failed.id) is None
        assert db_session.get(Simulation, recent_failed.id) is not None

    def test_pending_and_running_simulations_are_strictly_protected(self, db_session: Session):
        """Active jobs (pending or running) must NEVER be deleted by retention regardless of age."""
        user = create_test_user(db_session, "elig_active")
        old_pending = create_historical_simulation(db_session, user, STATUS_PENDING, age_days=30.0)
        old_running = create_historical_simulation(db_session, user, STATUS_RUNNING, age_days=30.0)
        old_completed = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=30.0)

        result = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert result.success is True
        assert result.deleted_count == 1

        # Only completed was deleted; pending and running are safely preserved
        assert db_session.get(Simulation, old_completed.id) is None
        assert db_session.get(Simulation, old_pending.id) is not None
        assert db_session.get(Simulation, old_running.id) is not None


class TestRetentionQuotaSemantics:
    """Verify that automated retention NEVER refunds quota."""

    def test_cleanup_does_not_refund_quota(self, db_session: Session):
        user = create_test_user(db_session, "quota_invariant")

        # Reserve usage for a 500-interaction simulation
        reserve_usage(db_session, user=user, requested_interactions=500, delta_requests=1)
        sim = create_historical_simulation(
            db_session, user, STATUS_COMPLETED, age_days=15.0, num_interactions=500
        )

        from app.db.models.usage import MonthlyUsage
        usage = db_session.execute(select(MonthlyUsage).where(MonthlyUsage.user_id == user.id)).scalar_one()
        initial_reqs = usage.request_count
        initial_inters = usage.interaction_count
        assert initial_reqs >= 1
        assert initial_inters >= 500

        # Execute cleanup
        result = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert result.deleted_count == 1
        assert db_session.get(Simulation, sim.id) is None

        # Verify quota is STRICTLY preserved (not refunded)
        db_session.refresh(usage)
        assert usage.request_count == initial_reqs
        assert usage.interaction_count == initial_inters

    def test_repeated_cleanup_does_not_alter_quota(self, db_session: Session):
        user = create_test_user(db_session, "quota_repeated")
        reserve_usage(db_session, user=user, requested_interactions=1000, delta_requests=1)
        create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=14.0, num_interactions=1000)

        from app.db.models.usage import MonthlyUsage
        usage = db_session.execute(select(MonthlyUsage).where(MonthlyUsage.user_id == user.id)).scalar_one()
        initial_reqs = usage.request_count
        initial_inters = usage.interaction_count

        # Run cleanup 3 times in succession
        for _ in range(3):
            cleanup_expired_simulations(db=db_session, retention_days=7)
            db_session.refresh(usage)
            assert usage.request_count == initial_reqs
            assert usage.interaction_count == initial_inters


class TestHistoryAndDetailSemantics:
    """Verify expired simulations behave like deleted simulations across API endpoints."""

    def test_expired_simulation_disappears_from_history_and_detail_returns_404(
        self, client, db_session: Session
    ):
        from app.services.api_key import create_api_key

        user = create_test_user(db_session, "api_history")
        key_res = create_api_key(db_session, user=user, name="Test Key")
        auth_headers = {"Authorization": f"Bearer {key_res.key}"}

        # Create 1 expired simulation and 1 recent simulation
        old_sim = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)
        recent_sim = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=1.0)

        # Before cleanup: 2 simulations in history
        resp = client.get("/v1/simulations", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

        # Execute cleanup
        cleanup_result = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert cleanup_result.deleted_count == 1

        # After cleanup: history contains only the recent run
        resp2 = client.get("/v1/simulations", headers=auth_headers)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["total"] == 1
        assert data2["items"][0]["simulation_id"] == str(recent_sim.id)

        # GET detail of expired simulation returns standard 404
        resp_detail = client.get(f"/v1/simulations/{old_sim.id}", headers=auth_headers)
        assert resp_detail.status_code == 404
        assert resp_detail.json()["error"]["details"]["code"] == "simulation_not_found"

        # GET detail of recent simulation returns 200
        resp_recent = client.get(f"/v1/simulations/{recent_sim.id}", headers=auth_headers)
        assert resp_recent.status_code == 200


class TestBatchBehaviorAndDryRun:
    """Verify bounded batch deletion, rollback safety, and dry-run reporting."""

    def test_bounded_batch_size_respected(self, db_session: Session):
        user = create_test_user(db_session, "batches")
        for _ in range(7):
            create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)

        # Batch size 3 over 7 items -> should execute 3 batches (3 + 3 + 1 = 7)
        result = cleanup_expired_simulations(db=db_session, retention_days=7, batch_size=3)
        assert result.success is True
        assert result.deleted_count == 7
        assert result.batch_count == 3

    def test_dry_run_reports_eligible_without_mutating_data(self, db_session: Session):
        user = create_test_user(db_session, "dry_run")
        s1 = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=12.0)
        s2 = create_historical_simulation(db_session, user, STATUS_FAILED, age_days=9.0)
        s3 = create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=2.0)

        result = cleanup_expired_simulations(db=db_session, retention_days=7, dry_run=True)
        assert result.success is True
        assert result.dry_run is True
        assert result.eligible_count == 2
        assert result.deleted_count == 0
        assert result.batch_count == 0

        # Assert zero rows were mutated
        assert db_session.get(Simulation, s1.id) is not None
        assert db_session.get(Simulation, s2.id) is not None
        assert db_session.get(Simulation, s3.id) is not None

    def test_idempotency_subsequent_runs_are_clean_noops(self, db_session: Session):
        user = create_test_user(db_session, "idempotent")
        for _ in range(3):
            create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)

        res1 = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert res1.deleted_count == 3

        res2 = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert res2.deleted_count == 0
        assert res2.eligible_count == 0

        res3 = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert res3.deleted_count == 0


class TestLargeResultsAndOperationalMetrics:
    """Verify memory safety on large payloads and metrics tracking."""

    def test_large_result_payload_deleted_safely(self, db_session: Session):
        user = create_test_user(db_session, "large_res")
        # Create a simulation with 5,000 synthetic records
        large_records = [{"idx": i, "val": f"payload_chunk_{i}"} for i in range(5000)]
        sim = create_historical_simulation(
            db_session, user, STATUS_COMPLETED, age_days=15.0, data=large_records
        )

        result = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert result.success is True
        assert result.deleted_count == 1
        assert db_session.get(Simulation, sim.id) is None

    def test_operational_metrics_updated_on_cleanup(self, db_session: Session):
        operational_metrics.reset()
        user = create_test_user(db_session, "metrics")
        create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)

        cleanup_expired_simulations(db=db_session, retention_days=7)

        summary = operational_metrics.get_summary()
        assert "retention" in summary
        assert summary["retention"]["cleanup_runs_total"] >= 1
        assert summary["retention"]["simulations_deleted_total"] >= 1
        assert summary["retention"]["last_deleted_count"] >= 1
        assert summary["retention"]["cleanup_failures_total"] == 0

    def test_diagnostics_endpoint_surfaces_retention_metrics(self, client, db_session: Session):
        operational_metrics.reset()
        user = create_test_user(db_session, "diag_retention")
        create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)
        cleanup_expired_simulations(db=db_session, retention_days=7)

        resp = client.get("/v1/diagnostics")
        assert resp.status_code == 200
        data = resp.json()
        assert "retention" in data["metrics"]
        assert data["metrics"]["retention"]["cleanup_runs_total"] >= 1


class TestRetentionConfigValidation:
    """Verify configuration boundaries for SIMULATION_RETENTION_DAYS and SIMULATION_CLEANUP_BATCH_SIZE."""

    def test_retention_days_must_be_positive(self):
        with pytest.raises(ValueError, match="SIMULATION_RETENTION_DAYS must be a positive integer"):
            Settings(SIMULATION_RETENTION_DAYS=0)

    def test_batch_size_must_be_bounded(self):
        with pytest.raises(ValueError, match="SIMULATION_CLEANUP_BATCH_SIZE must be between 1 and 1000"):
            Settings(SIMULATION_CLEANUP_BATCH_SIZE=0)
        with pytest.raises(ValueError, match="SIMULATION_CLEANUP_BATCH_SIZE must be between 1 and 1000"):
            Settings(SIMULATION_CLEANUP_BATCH_SIZE=5000)


class TestConcurrentCleanupAndCLI:
    """Verify concurrent execution safety, error handling, and CLI entrypoint."""

    def test_concurrent_cleanup_executions_remain_safe(self, tmp_path):
        from pathlib import Path
        from sqlalchemy import text, create_engine
        from app.db.models import Base

        db_file = tmp_path / "concurrent_retention.db"
        engine = create_engine(
            f"sqlite:///{db_file}",
            connect_args={"timeout": 30.0},
        )
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=15000;"))
            conn.commit()

        Base.metadata.create_all(bind=engine)
        ConcurrentSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

        with ConcurrentSessionLocal() as session:
            user = create_test_user(session, "concurrent_clean")
            sim_ids = []
            for _ in range(20):
                s = create_historical_simulation(session, user, STATUS_COMPLETED, age_days=10.0)
                sim_ids.append(s.id)

        # Run 2 cleanup tasks in parallel against ConcurrentSessionLocal
        def run_worker(wid: int):
            return cleanup_expired_simulations(
                session_factory=ConcurrentSessionLocal,
                retention_days=7,
                batch_size=5,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_worker, wid) for wid in range(2)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for r in results:
            assert r.success is True

        total_deleted = sum(r.deleted_count for r in results)
        assert total_deleted == 20

        # All 20 simulations deleted
        with ConcurrentSessionLocal() as session:
            remaining = session.execute(
                select(func.count(Simulation.id)).where(Simulation.id.in_(sim_ids))
            ).scalar()
            assert remaining == 0

    def test_cli_dry_run_invoked_successfully(self):
        cmd = [sys.executable, "-m", "app.cleanup", "--dry-run", "--retention-days", "7"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0
        assert "BehaviorSim Simulation Data Retention Cleanup" in proc.stdout
        assert "DRY RUN" in proc.stdout

    def test_cleanup_error_returns_failure_result(self, monkeypatch, db_session: Session):
        user = create_test_user(db_session, "err_user")
        create_historical_simulation(db_session, user, STATUS_COMPLETED, age_days=10.0)

        def mock_delete_batch(*args, **kwargs):
            raise RuntimeError("Simulated database failure during batch delete")

        monkeypatch.setattr("app.services.retention._delete_single_batch", mock_delete_batch)

        res = cleanup_expired_simulations(db=db_session, retention_days=7)
        assert res.success is False
        assert "Simulated database failure" in res.error_message

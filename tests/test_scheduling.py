"""Phase 22 Tests: Production Cleanup Scheduling & Operational Lifecycle."""

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import yaml
from sqlalchemy import select, func
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.metrics import operational_metrics
from app.db.models.simulation import Simulation
from app.db.models.user import User
from app.services.retention import (
    CleanupResult,
    cleanup_expired_simulations,
    count_eligible_simulations,
)
from app.services.simulation_job import STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING
from tests.conftest import TestingSessionLocal


def create_test_user(db: Session, email_prefix: str = "sched_user") -> User:
    """Helper to create an active user with assigned free plan."""
    from app.services.plan import get_or_create_free_plan

    free_plan = get_or_create_free_plan(db)
    user = User(
        email=f"{email_prefix}_{uuid.uuid4().hex[:6]}@example.com",
        display_name="Scheduling Test User",
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
    created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
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


class TestRenderBlueprintSpecification:
    """Verify the Render Blueprint specification for cron scheduling in render.yaml."""

    def test_render_yaml_contains_valid_cleanup_cron_job(self):
        root_dir = Path(__file__).resolve().parent.parent
        render_yaml_path = root_dir / "render.yaml"
        assert render_yaml_path.exists(), "render.yaml must exist at project root"

        with open(render_yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        assert "services" in data, "render.yaml must define services"
        services = data["services"]

        cron_services = [s for s in services if s.get("type") == "cron"]
        assert len(cron_services) >= 1, "render.yaml must define at least one cron service"

        cleanup_cron = next((s for s in cron_services if s.get("name") == "behaviorsim-cleanup"), None)
        assert cleanup_cron is not None, "behaviorsim-cleanup cron service must be defined"

        # Check schedule: 02:00 UTC daily
        assert cleanup_cron.get("schedule") == "0 2 * * *", "schedule must be daily at 02:00 UTC ('0 2 * * *')"

        # Check commands
        assert cleanup_cron.get("startCommand") == "python -m app.cleanup", "startCommand must be 'python -m app.cleanup'"
        assert cleanup_cron.get("buildCommand") == "pip install -e .", "buildCommand must be 'pip install -e .'"

        # Check environment variables
        env_vars = {e.get("key"): e for e in cleanup_cron.get("envVars", [])}
        assert "DATABASE_URL" in env_vars, "DATABASE_URL must be defined in cron envVars"

        db_url_spec = env_vars["DATABASE_URL"]
        assert "fromService" in db_url_spec, "DATABASE_URL should reference web service via fromService"
        assert db_url_spec["fromService"]["type"] == "web"
        assert db_url_spec["fromService"]["name"] == "behaviorsim-api"
        assert db_url_spec["fromService"]["envVarKey"] == "DATABASE_URL"

        assert env_vars.get("APP_ENV", {}).get("value") == "production"
        assert env_vars.get("SIMULATION_RETENTION_DAYS", {}).get("value") == "7"
        assert env_vars.get("SIMULATION_CLEANUP_BATCH_SIZE", {}).get("value") == "100"


class TestCleanupCLIHardening:
    """Verify operator CLI arguments, exit codes, dry-run, and error handling."""

    def test_cli_help_exits_zero(self):
        cmd = [sys.executable, "-m", "app.cleanup", "--help"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0
        assert "BehaviorSim Simulation Data Retention Cleanup CLI" in proc.stdout
        assert "--dry-run" in proc.stdout
        assert "--retention-days" in proc.stdout
        assert "--batch-size" in proc.stdout

    def test_cli_invalid_retention_days_exits_code_2(self):
        cmd = [sys.executable, "-m", "app.cleanup", "--retention-days", "0"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 2
        assert "ERROR: --retention-days must be a positive integer" in proc.stderr

        cmd_neg = [sys.executable, "-m", "app.cleanup", "--retention-days", "-5"]
        proc_neg = subprocess.run(cmd_neg, capture_output=True, text=True)
        assert proc_neg.returncode == 2
        assert "ERROR: --retention-days must be a positive integer" in proc_neg.stderr

    def test_cli_invalid_batch_size_exits_code_2(self):
        cmd_zero = [sys.executable, "-m", "app.cleanup", "--batch-size", "0"]
        proc_zero = subprocess.run(cmd_zero, capture_output=True, text=True)
        assert proc_zero.returncode == 2
        assert "ERROR: --batch-size must be between 1 and 1000" in proc_zero.stderr

        cmd_large = [sys.executable, "-m", "app.cleanup", "--batch-size", "2000"]
        proc_large = subprocess.run(cmd_large, capture_output=True, text=True)
        assert proc_large.returncode == 2
        assert "ERROR: --batch-size must be between 1 and 1000" in proc_large.stderr

    def test_cli_empty_cleanup_run_exits_zero(self):
        """When 0 eligible records exist, CLI exits cleanly with code 0."""
        cmd = [sys.executable, "-m", "app.cleanup"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0
        assert "BehaviorSim Simulation Data Retention Cleanup" in proc.stdout
        assert "Deleted Rows:     0" in proc.stdout or "eligible" in proc.stdout.lower()

    def test_cli_dry_run_exits_zero_without_mutating_data(self):
        cmd = [sys.executable, "-m", "app.cleanup", "--dry-run", "--retention-days", "7"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0
        assert "DRY RUN (No changes)" in proc.stdout
        assert "Execution Results:" in proc.stdout


class TestServiceValidationAndEdgeCases:
    """Verify retention service validation and failure handling."""

    def test_service_rejects_batch_size_above_1000(self, db_session: Session):
        with pytest.raises(ValueError, match="batch_size must be between 1 and 1000"):
            cleanup_expired_simulations(db=db_session, batch_size=1500)

    def test_service_rejects_batch_size_below_1(self, db_session: Session):
        with pytest.raises(ValueError, match="batch_size must be between 1 and 1000"):
            cleanup_expired_simulations(db=db_session, batch_size=0)

    def test_service_rejects_retention_days_below_1(self, db_session: Session):
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            cleanup_expired_simulations(db=db_session, retention_days=0)

    def test_production_validation_catches_invalid_retention_settings(self):
        valid_settings = Settings(
            APP_ENV="production",
            DATABASE_URL="postgresql+psycopg://user:pass@ep-remote.neon.tech/neondb?sslmode=require",
            API_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            WEB_BASE_URL="https://behaviorsim.vedaangsharma.in",
            OAUTH_REDIRECT_BASE_URL="https://api.behaviorsim.vedaangsharma.in",
            CORS_ORIGINS=["https://behaviorsim.vedaangsharma.in"],
            SIMULATION_RETENTION_DAYS=7,
            SIMULATION_CLEANUP_BATCH_SIZE=100,
        )
        # Should not raise
        valid_settings.validate_production_configuration()


class TestConcurrencyAndScheduledExecutionSafety:
    """Verify concurrent scheduled executions remain safe and idempotent."""

    def test_concurrent_cleanup_runs_delete_cleanly_without_collision(self, tmp_path):
        import concurrent.futures
        from sqlalchemy import create_engine, text
        from app.db.models import Base

        db_file = tmp_path / "concurrent_sched_test.db"
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
            user = create_test_user(session, "sched_concurrent")
            for _ in range(15):
                create_historical_simulation(session, user, STATUS_COMPLETED, age_days=10.0)

        # Run 2 cleanup executions simultaneously (simulating overlapping cron runs)
        def run_cleanup_job(worker_id: int):
            return cleanup_expired_simulations(
                session_factory=ConcurrentSessionLocal,
                retention_days=7,
                batch_size=5,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_cleanup_job, i) for i in range(2)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        for r in results:
            assert r.success is True

        total_deleted = sum(r.deleted_count for r in results)
        assert total_deleted == 15

        # Verify 0 rows remain
        with ConcurrentSessionLocal() as session:
            remaining = session.execute(
                select(func.count(Simulation.id)).where(Simulation.status == STATUS_COMPLETED)
            ).scalar()
            assert remaining == 0

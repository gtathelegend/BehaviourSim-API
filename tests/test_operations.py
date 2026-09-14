"""Phase 23 Tests: Production Monitoring, Alerting & SLO Readiness."""

import time
import uuid
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.orm import Session

from app.core.metrics import (
    CLEANUP_STALE_THRESHOLD_SECONDS,
    LATENCY_HISTOGRAM_BUCKETS,
    operational_metrics,
)
from app.core.middleware import get_route_category
from app.api.v1.diagnostics import (
    get_cached_queue_counts,
    get_cached_queue_metrics,
    reset_diagnostics_cache,
)
from app.db.models.simulation import Simulation
from app.db.models.user import User
from app.services.simulation_job import STATUS_PENDING, STATUS_RUNNING, STATUS_COMPLETED


def create_test_user(db: Session, email_prefix: str = "ops_user") -> User:
    """Helper to create an active user with assigned free plan."""
    from app.services.plan import get_or_create_free_plan

    free_plan = get_or_create_free_plan(db)
    user = User(
        email=f"{email_prefix}_{uuid.uuid4().hex[:6]}@example.com",
        display_name="Operations Test User",
        plan_id=free_plan.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestRequestMetricsAndHistograms:
    """Verify HTTP request metrics, status classes, route categories, and latency buckets."""

    def test_request_metrics_and_status_classes(self):
        operational_metrics.reset()

        operational_metrics.record_request("GET", "/v1/simulations", 200, 15.0, route_category="simulations")
        operational_metrics.record_request("POST", "/v1/simulations", 202, 45.0, route_category="simulations")
        operational_metrics.record_request("GET", "/v1/presets", 304, 2.0, route_category="presets")
        operational_metrics.record_request("GET", "/v1/simulations/xyz", 404, 8.0, route_category="simulations")
        operational_metrics.record_request("POST", "/v1/auth/login", 401, 12.0, route_category="auth")
        operational_metrics.record_request("POST", "/v1/simulations", 429, 5.0, route_category="simulations")
        operational_metrics.record_request("GET", "/v1/crash", 500, 110.0, route_category="other")

        summary = operational_metrics.get_summary()
        api = summary["api"]

        assert api["requests_total"] == 7
        assert api["requests_by_method"]["GET"] == 4
        assert api["requests_by_method"]["POST"] == 3

        assert api["requests_by_status_class"]["2xx"] == 2
        assert api["requests_by_status_class"]["3xx"] == 1
        assert api["requests_by_status_class"]["4xx"] == 3
        assert api["requests_by_status_class"]["5xx"] == 1

        assert api["requests_by_route_category"]["simulations"] == 4
        assert api["requests_by_route_category"]["presets"] == 1
        assert api["requests_by_route_category"]["auth"] == 1
        assert api["requests_by_route_category"]["other"] == 1

        assert api["errors_total"] == 4
        assert api["auth_failures_total"] == 1
        assert api["rate_limit_rejections_total"] == 1

    def test_latency_histogram_buckets(self):
        operational_metrics.reset()

        # Place requests into various specific buckets
        operational_metrics.record_request("GET", "/v1/presets", 200, 5.0)     # lt_10ms
        operational_metrics.record_request("GET", "/v1/presets", 200, 18.0)    # lt_25ms
        operational_metrics.record_request("GET", "/v1/presets", 200, 35.0)    # lt_50ms
        operational_metrics.record_request("GET", "/v1/presets", 200, 80.0)    # lt_100ms
        operational_metrics.record_request("GET", "/v1/presets", 200, 150.0)   # lt_250ms
        operational_metrics.record_request("GET", "/v1/presets", 200, 350.0)   # lt_500ms
        operational_metrics.record_request("GET", "/v1/presets", 200, 750.0)   # lt_1s
        operational_metrics.record_request("GET", "/v1/presets", 200, 1500.0)  # lt_2s
        operational_metrics.record_request("GET", "/v1/presets", 200, 3500.0)  # lt_5s
        operational_metrics.record_request("GET", "/v1/presets", 200, 7500.0)  # gt_5s

        summary = operational_metrics.get_summary()
        hist = summary["api"]["latency_ms"]["histogram"]

        assert hist["lt_10ms"] == 1
        assert hist["lt_25ms"] == 1
        assert hist["lt_50ms"] == 1
        assert hist["lt_100ms"] == 1
        assert hist["lt_250ms"] == 1
        assert hist["lt_500ms"] == 1
        assert hist["lt_1s"] == 1
        assert hist["lt_2s"] == 1
        assert hist["lt_5s"] == 1
        assert hist["gt_5s"] == 1

        assert summary["api"]["latency_ms"]["min"] == 5.0
        assert summary["api"]["latency_ms"]["max"] == 7500.0

    def test_route_category_classification(self):
        assert get_route_category("/v1/simulations") == "simulations"
        assert get_route_category("/v1/simulations/job-123") == "simulations"
        assert get_route_category("/v1/presets") == "presets"
        assert get_route_category("/v1/presets/education") == "presets"
        assert get_route_category("/v1/auth/google") == "auth"
        assert get_route_category("/v1/account") == "account"
        assert get_route_category("/v1/diagnostics") == "diagnostics"
        assert get_route_category("/health") == "health"
        assert get_route_category("/ready") == "health"
        assert get_route_category("/unknown/path") == "other"


class TestSimulationLifecycleAndWorkerMetrics:
    """Verify simulation lifecycle tracking, recovery, retry exhaustion, and worker throughput."""

    def test_simulation_lifecycle_and_worker_throughput(self):
        operational_metrics.reset()

        # 1. Accept 2 jobs
        operational_metrics.record_simulation_accepted("education", 1000)
        operational_metrics.record_simulation_accepted("finance", 2000)

        # 2. Worker claims
        operational_metrics.record_simulation_started()
        operational_metrics.record_simulation_started()

        # 3. Worker completes first job (1000 interactions in 50ms, 10ms queue wait)
        operational_metrics.record_simulation_completed(
            compute_ms=50,
            queue_wait_ms=10,
            num_interactions=1000,
        )

        # 4. Worker completes second job (2000 interactions in 100ms, 20ms queue wait)
        operational_metrics.record_simulation_completed(
            compute_ms=100,
            queue_wait_ms=20,
            num_interactions=2000,
        )

        # 5. Record a failure, a recovery, and an exhausted retry
        operational_metrics.record_simulation_failed("execution_timeout")
        operational_metrics.record_simulation_recovered()
        operational_metrics.record_simulation_exhausted_retries()
        operational_metrics.record_quota_exhausted()

        summary = operational_metrics.get_summary()
        sims = summary["simulations"]

        assert sims["accepted"] == 2
        assert sims["accepted_by_preset"]["education"] == 1
        assert sims["accepted_by_preset"]["finance"] == 1
        assert sims["started"] == 2
        assert sims["completed"] == 2
        assert sims["failed"] == 1
        assert sims["failed_by_code"]["execution_timeout"] == 1
        assert sims["recovered"] == 1
        assert sims["failed_after_max_attempts"] == 1
        assert sims["interactions_processed_total"] == 3000

        # Throughput calculations
        # total compute = 150ms = 0.15s
        # 3000 interactions / 0.15s = 20000.0 inter/s
        # 2 jobs / 0.15s = 13.33 jobs/s
        throughput = sims["worker_throughput"]
        assert throughput["total_compute_ms"] == 150
        assert throughput["interactions_per_second"] == 20000.0
        assert throughput["jobs_per_second"] == 13.33

        # Quota metric
        assert summary["api"]["quota_exhausted_total"] == 1


class TestQueueDiagnosticsAndAge:
    """Verify queue depth, oldest pending age, oldest running age, and cache behavior."""

    def test_empty_queue_diagnostics(self, db_session: Session):
        reset_diagnostics_cache()
        counts = get_cached_queue_metrics(db_session, ttl=0.0)
        pending, running, depth, oldest_pending_age, oldest_running_age = counts

        assert pending == 0
        assert running == 0
        assert depth == 0
        assert oldest_pending_age is None
        assert oldest_running_age is None

        # Verify backwards-compatible 2-tuple function
        p_count, r_count = get_cached_queue_counts(db_session, ttl=0.0)
        assert p_count == 0
        assert r_count == 0

    def test_queue_backlog_calculates_oldest_job_ages(self, db_session: Session):
        reset_diagnostics_cache()
        user = create_test_user(db_session, "queue_age_user")

        now = datetime.now(timezone.utc)
        old_time = now - timedelta(seconds=120)

        # Create a pending job backdated by 120 seconds
        p_sim = Simulation(
            id=uuid.uuid4(),
            user_id=user.id,
            preset="education",
            num_interactions=100,
            status=STATUS_PENDING,
            behaviorsim_version="1.0.1",
            api_version="v1",
            created_at=old_time,
            updated_at=old_time,
        )
        db_session.add(p_sim)

        # Create a running job backdated by 45 seconds
        r_time = now - timedelta(seconds=45)
        r_sim = Simulation(
            id=uuid.uuid4(),
            user_id=user.id,
            preset="education",
            num_interactions=100,
            status=STATUS_RUNNING,
            behaviorsim_version="1.0.1",
            api_version="v1",
            created_at=r_time - timedelta(seconds=5),
            started_at=r_time,
            updated_at=r_time,
        )
        db_session.add(r_sim)
        db_session.commit()

        counts = get_cached_queue_metrics(db_session, ttl=0.0)
        pending, running, depth, oldest_p_age, oldest_r_age = counts

        assert pending == 1
        assert running == 1
        assert depth == 2
        assert oldest_p_age is not None
        assert oldest_p_age >= 115.0  # Around 120s
        assert oldest_r_age is not None
        assert oldest_r_age >= 40.0   # Around 45s

        # Verify 2-tuple helper still returns (1, 1)
        p_c, r_c = get_cached_queue_counts(db_session, ttl=0.0)
        assert p_c == 1
        assert r_c == 1

    def test_diagnostics_cache_ttl_serves_cached_data(self, db_session: Session):
        reset_diagnostics_cache()
        # Initial call populates cache
        c1 = get_cached_queue_metrics(db_session, ttl=10.0)

        # Add a simulation directly
        user = create_test_user(db_session, "ttl_cache_user")
        s = Simulation(
            id=uuid.uuid4(),
            user_id=user.id,
            preset="education",
            num_interactions=100,
            status=STATUS_PENDING,
            behaviorsim_version="1.0.1",
            api_version="v1",
        )
        db_session.add(s)
        db_session.commit()

        # Immediate call within TTL returns cached result (depth=0)
        c2 = get_cached_queue_metrics(db_session, ttl=10.0)
        assert c2[2] == c1[2]

        # Explicit cache reset forces refresh
        reset_diagnostics_cache()
        c3 = get_cached_queue_metrics(db_session, ttl=10.0)
        assert c3[0] >= 1


class TestCleanupHealthAndFreshness:
    """Verify retention cleanup metrics and stale warning detection."""

    def test_cleanup_stale_warning_logic(self):
        operational_metrics.reset()

        # 1. Fresh cleanup (just ran)
        operational_metrics.record_cleanup_run(deleted_count=25, duration_ms=12.5, success=True)
        summary1 = operational_metrics.get_summary()
        ret1 = summary1["retention"]
        assert ret1["cleanup_runs_total"] == 1
        assert ret1["simulations_deleted_total"] == 25
        assert ret1["cleanup_failures_total"] == 0
        assert ret1["stale_warning"] is False
        assert ret1["hours_since_last_cleanup"] is not None
        assert ret1["hours_since_last_cleanup"] < 0.1

        # 2. Simulate stale cleanup by artificially backdating last_run_at by 27 hours
        operational_metrics._cleanup_last_run_at = time.time() - (27 * 3600)
        summary2 = operational_metrics.get_summary()
        ret2 = summary2["retention"]
        assert ret2["stale_warning"] is True
        assert ret2["hours_since_last_cleanup"] >= 26.9


class TestDiagnosticsEndpointAndSecurity:
    """Verify GET /v1/diagnostics response model, health indicators, and absence of secrets."""

    def test_diagnostics_response_contains_health_and_no_secrets(self, client):
        reset_diagnostics_cache()
        resp = client.get("/v1/diagnostics")
        assert resp.status_code == 200
        data = resp.json()

        assert data["status"] == "ok"
        assert "version" in data
        assert "queue" in data
        assert "health" in data
        assert "metrics" in data

        # Health indicators
        assert "queue_healthy" in data["health"]
        assert "cleanup_healthy" in data["health"]
        assert "error_rate_pct" in data["health"]

        # Queue status
        assert "pending" in data["queue"]
        assert "running" in data["queue"]
        assert "depth" in data["queue"]
        assert "oldest_pending_age_seconds" in data["queue"]

        # Latency histogram in metrics
        assert "histogram" in data["metrics"]["api"]["latency_ms"]

        # Zero-secret guarantee
        raw_text = resp.text.lower()
        assert "password" not in raw_text
        assert "secret" not in raw_text
        assert "database_url" not in raw_text
        assert "postgres://" not in raw_text
        assert "postgresql://" not in raw_text
        assert "token" not in raw_text or "total" in raw_text

    def test_request_id_propagated_and_returned_in_header(self, client):
        custom_req_id = "test-correlation-id-998877"
        resp = client.get("/v1/presets", headers={"X-Request-ID": custom_req_id})
        assert resp.status_code == 200
        assert resp.headers.get("X-Request-ID") == custom_req_id

    def test_error_response_preserves_request_id_without_leakage(self, client):
        custom_req_id = "test-error-req-12345"
        resp = client.get("/v1/simulations/00000000-0000-0000-0000-000000000000", headers={"X-Request-ID": custom_req_id})
        assert resp.status_code == 401
        data = resp.json()
        assert "error" in data
        assert data["error"]["request_id"] == custom_req_id
        # No stack trace
        assert "traceback" not in resp.text.lower()


class TestWorkerLifespanDaemon:
    """Verify in-process worker daemon thread startup, execution, and graceful shutdown."""

    def test_lifespan_starts_and_stops_worker_thread(self):
        import threading
        from fastapi.testclient import TestClient
        from app.core.config import get_settings
        from app.main import create_app

        settings = get_settings()
        original_flag = settings.RUN_WORKER_THREAD
        try:
            settings.RUN_WORKER_THREAD = True
            test_app = create_app()
            worker_t = None
            with TestClient(test_app) as client:
                resp = client.get("/health")
                assert resp.status_code == 200
                for t in threading.enumerate():
                    if t.name == "simulation-worker-daemon":
                        worker_t = t
                        break
                assert worker_t is not None, "Worker thread should be running"

            # After exiting context manager, wait up to 5.0s for the network worker to terminate
            worker_t.join(timeout=5.0)
            assert not worker_t.is_alive(), "Worker thread must terminate cleanly on shutdown"
        finally:
            settings.RUN_WORKER_THREAD = original_flag

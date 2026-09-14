"""Thread-safe in-process operational metrics collector for BehaviorSim API and workers."""

import threading
import time
from typing import Any, Dict, Optional, Tuple

# Bounded latency histogram thresholds in milliseconds
LATENCY_HISTOGRAM_BUCKETS: Tuple[Tuple[str, float], ...] = (
    ("lt_10ms", 10.0),
    ("lt_25ms", 25.0),
    ("lt_50ms", 50.0),
    ("lt_100ms", 100.0),
    ("lt_250ms", 250.0),
    ("lt_500ms", 500.0),
    ("lt_1s", 1000.0),
    ("lt_2s", 2000.0),
    ("lt_5s", 5000.0),
    ("gt_5s", float("inf")),
)

# Standard tolerance for daily retention cleanup (24h schedule + 2h operational tolerance)
CLEANUP_STALE_THRESHOLD_SECONDS = 93600.0  # 26 hours


class OperationalMetrics:
    """Thread-safe collector for operational diagnostics and simulation execution metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self.reset()

    def reset(self) -> None:
        """Reset all counters and latency distributions (useful for testing)."""
        with getattr(self, "_lock", threading.Lock()):
            self._started_at = time.time()

            # HTTP Request Metrics
            self._api_requests_total = 0
            self._api_requests_by_method: Dict[str, int] = {}
            self._api_requests_by_status_class: Dict[str, int] = {
                "2xx": 0,
                "3xx": 0,
                "4xx": 0,
                "5xx": 0,
            }
            self._api_requests_by_route_category: Dict[str, int] = {}
            self._api_errors_total = 0
            self._api_errors_by_status: Dict[int, int] = {}
            self._auth_failures_total = 0
            self._rate_limit_rejections_total = 0
            self._quota_exhausted_total = 0

            # HTTP Request Latency Bounded Distribution
            self._api_request_duration_count = 0
            self._api_request_duration_total_ms = 0.0
            self._api_request_duration_min_ms: Optional[float] = None
            self._api_request_duration_max_ms: Optional[float] = None
            self._api_latency_histogram: Dict[str, int] = {
                bucket_key: 0 for bucket_key, _ in LATENCY_HISTOGRAM_BUCKETS
            }

            # Simulation Lifecycle Metrics
            self._simulations_accepted = 0
            self._simulations_accepted_by_preset: Dict[str, int] = {}
            self._simulations_started = 0
            self._simulations_completed = 0
            self._simulations_failed = 0
            self._simulations_failed_by_code: Dict[str, int] = {}
            self._simulations_recovered = 0
            self._simulations_failed_after_max_attempts = 0
            self._interactions_processed_total = 0
            self._worker_compute_total_ms = 0

            # Latency distributions (execution duration & queue wait)
            self._sim_duration_count = 0
            self._sim_duration_total_ms = 0
            self._sim_duration_min_ms: Optional[int] = None
            self._sim_duration_max_ms: Optional[int] = None

            self._queue_wait_count = 0
            self._queue_wait_total_ms = 0
            self._queue_wait_min_ms: Optional[int] = None
            self._queue_wait_max_ms: Optional[int] = None

            # Data Lifecycle & Retention Metrics
            self._cleanup_runs_total = 0
            self._cleanup_simulations_deleted_total = 0
            self._cleanup_failures_total = 0
            self._cleanup_duration_total_ms = 0.0
            self._cleanup_last_run_at: Optional[float] = None
            self._cleanup_last_deleted_count: int = 0

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        route_category: str = "other",
    ) -> None:
        """Record an incoming HTTP request, outcome, and bounded latency bucket."""
        with self._lock:
            self._api_requests_total += 1
            meth = method.upper()
            self._api_requests_by_method[meth] = self._api_requests_by_method.get(meth, 0) + 1

            # Categorize status code class
            if 200 <= status_code < 300:
                self._api_requests_by_status_class["2xx"] += 1
            elif 300 <= status_code < 400:
                self._api_requests_by_status_class["3xx"] += 1
            elif 400 <= status_code < 500:
                self._api_requests_by_status_class["4xx"] += 1
            elif status_code >= 500:
                self._api_requests_by_status_class["5xx"] += 1

            # Low-cardinality route category
            category = route_category or "other"
            self._api_requests_by_route_category[category] = (
                self._api_requests_by_route_category.get(category, 0) + 1
            )

            # Latency histogram bucket
            dur = max(0.0, duration_ms)
            for bucket_key, threshold in LATENCY_HISTOGRAM_BUCKETS:
                if dur < threshold:
                    self._api_latency_histogram[bucket_key] += 1
                    break

            # Aggregate request duration
            self._api_request_duration_count += 1
            self._api_request_duration_total_ms += dur
            if self._api_request_duration_min_ms is None or dur < self._api_request_duration_min_ms:
                self._api_request_duration_min_ms = dur
            if self._api_request_duration_max_ms is None or dur > self._api_request_duration_max_ms:
                self._api_request_duration_max_ms = dur

            # Errors and rejections
            if status_code >= 400:
                self._api_errors_total += 1
                self._api_errors_by_status[status_code] = (
                    self._api_errors_by_status.get(status_code, 0) + 1
                )

            if status_code in (401, 403):
                self._auth_failures_total += 1
            elif status_code == 429:
                self._rate_limit_rejections_total += 1

    def record_quota_exhausted(self) -> None:
        """Record rejection of a simulation submission due to quota exhaustion."""
        with self._lock:
            self._quota_exhausted_total += 1

    def record_simulation_accepted(self, preset: str, num_interactions: int) -> None:
        """Record acceptance of a new simulation job."""
        with self._lock:
            self._simulations_accepted += 1
            self._simulations_accepted_by_preset[preset] = (
                self._simulations_accepted_by_preset.get(preset, 0) + 1
            )

    def record_simulation_started(self) -> None:
        """Record worker atomic claim and execution start of a simulation job."""
        with self._lock:
            self._simulations_started += 1

    def record_simulation_recovered(self) -> None:
        """Record worker recovery of an abandoned or lease-expired running job."""
        with self._lock:
            self._simulations_recovered += 1

    def record_simulation_exhausted_retries(self) -> None:
        """Record permanent failure of a simulation job after exceeding maximum attempts."""
        with self._lock:
            self._simulations_failed_after_max_attempts += 1

    def record_simulation_completed(
        self,
        compute_ms: int,
        queue_wait_ms: Optional[int] = None,
        num_interactions: int = 0,
    ) -> None:
        """Record successful worker completion of a simulation job."""
        with self._lock:
            self._simulations_completed += 1
            if num_interactions > 0:
                self._interactions_processed_total += num_interactions

            # Compute execution duration distribution
            if compute_ms is not None and compute_ms >= 0:
                self._sim_duration_count += 1
                self._sim_duration_total_ms += compute_ms
                self._worker_compute_total_ms += compute_ms
                if self._sim_duration_min_ms is None or compute_ms < self._sim_duration_min_ms:
                    self._sim_duration_min_ms = compute_ms
                if self._sim_duration_max_ms is None or compute_ms > self._sim_duration_max_ms:
                    self._sim_duration_max_ms = compute_ms

            # Queue wait distribution
            if queue_wait_ms is not None and queue_wait_ms >= 0:
                self._queue_wait_count += 1
                self._queue_wait_total_ms += queue_wait_ms
                if self._queue_wait_min_ms is None or queue_wait_ms < self._queue_wait_min_ms:
                    self._queue_wait_min_ms = queue_wait_ms
                if self._queue_wait_max_ms is None or queue_wait_ms > self._queue_wait_max_ms:
                    self._queue_wait_max_ms = queue_wait_ms

    def record_simulation_failed(self, error_code: str) -> None:
        """Record failure of a simulation job."""
        with self._lock:
            self._simulations_failed += 1
            code = error_code or "unknown"
            self._simulations_failed_by_code[code] = (
                self._simulations_failed_by_code.get(code, 0) + 1
            )

    def record_cleanup_run(
        self,
        deleted_count: int,
        duration_ms: float,
        success: bool = True,
    ) -> None:
        """Record an execution of simulation retention data cleanup."""
        with self._lock:
            self._cleanup_runs_total += 1
            self._cleanup_last_run_at = time.time()
            self._cleanup_duration_total_ms += duration_ms
            if success:
                self._cleanup_simulations_deleted_total += deleted_count
                self._cleanup_last_deleted_count = deleted_count
            else:
                self._cleanup_failures_total += 1

    def get_summary(self) -> Dict[str, Any]:
        """Generate a complete operational metrics snapshot dictionary."""
        with self._lock:
            now = time.time()
            uptime_seconds = int(now - self._started_at)

            # Compute average request latency safely
            avg_request_duration_ms = (
                round(self._api_request_duration_total_ms / self._api_request_duration_count, 2)
                if self._api_request_duration_count > 0
                else 0.0
            )

            # Compute average simulation latencies safely
            avg_duration_ms = (
                round(self._sim_duration_total_ms / self._sim_duration_count, 1)
                if self._sim_duration_count > 0
                else 0.0
            )
            avg_queue_wait_ms = (
                round(self._queue_wait_total_ms / self._queue_wait_count, 1)
                if self._queue_wait_count > 0
                else 0.0
            )

            # Worker throughput rates
            worker_compute_sec = self._worker_compute_total_ms / 1000.0
            interactions_per_sec = (
                round(self._interactions_processed_total / worker_compute_sec, 1)
                if worker_compute_sec > 0
                else 0.0
            )
            jobs_per_sec = (
                round(self._simulations_completed / worker_compute_sec, 2)
                if worker_compute_sec > 0
                else 0.0
            )

            # Retention cleanup freshness
            hours_since_cleanup: Optional[float] = None
            stale_cleanup = False
            if self._cleanup_last_run_at is not None:
                elapsed = now - self._cleanup_last_run_at
                hours_since_cleanup = round(elapsed / 3600.0, 2)
                stale_cleanup = elapsed > CLEANUP_STALE_THRESHOLD_SECONDS

            return {
                "uptime_seconds": uptime_seconds,
                "api": {
                    "requests_total": self._api_requests_total,
                    "requests_by_method": dict(self._api_requests_by_method),
                    "requests_by_status_class": dict(self._api_requests_by_status_class),
                    "requests_by_route_category": dict(self._api_requests_by_route_category),
                    "errors_total": self._api_errors_total,
                    "errors_by_status": dict(self._api_errors_by_status),
                    "auth_failures_total": self._auth_failures_total,
                    "rate_limit_rejections_total": self._rate_limit_rejections_total,
                    "quota_exhausted_total": self._quota_exhausted_total,
                    "latency_ms": {
                        "count": self._api_request_duration_count,
                        "min": round(self._api_request_duration_min_ms or 0.0, 2),
                        "max": round(self._api_request_duration_max_ms or 0.0, 2),
                        "avg": avg_request_duration_ms,
                        "histogram": dict(self._api_latency_histogram),
                    },
                },
                "simulations": {
                    "accepted": self._simulations_accepted,
                    "accepted_by_preset": dict(self._simulations_accepted_by_preset),
                    "started": self._simulations_started,
                    "completed": self._simulations_completed,
                    "failed": self._simulations_failed,
                    "failed_by_code": dict(self._simulations_failed_by_code),
                    "recovered": self._simulations_recovered,
                    "failed_after_max_attempts": self._simulations_failed_after_max_attempts,
                    "interactions_processed_total": self._interactions_processed_total,
                    "worker_throughput": {
                        "interactions_per_second": interactions_per_sec,
                        "jobs_per_second": jobs_per_sec,
                        "total_compute_ms": self._worker_compute_total_ms,
                    },
                    "execution_duration_ms": {
                        "count": self._sim_duration_count,
                        "min": self._sim_duration_min_ms or 0,
                        "max": self._sim_duration_max_ms or 0,
                        "avg": avg_duration_ms,
                    },
                    "queue_wait_ms": {
                        "count": self._queue_wait_count,
                        "min": self._queue_wait_min_ms or 0,
                        "max": self._queue_wait_max_ms or 0,
                        "avg": avg_queue_wait_ms,
                    },
                },
                "retention": {
                    "cleanup_runs_total": self._cleanup_runs_total,
                    "simulations_deleted_total": self._cleanup_simulations_deleted_total,
                    "cleanup_failures_total": self._cleanup_failures_total,
                    "last_run_at": self._cleanup_last_run_at,
                    "last_deleted_count": self._cleanup_last_deleted_count,
                    "total_duration_ms": round(self._cleanup_duration_total_ms, 2),
                    "hours_since_last_cleanup": hours_since_cleanup,
                    "stale_warning": stale_cleanup,
                },
            }


# Singleton operational metrics instance
operational_metrics = OperationalMetrics()

"""Thread-safe in-process operational metrics collector for BehaviorSim API and workers."""

import threading
import time
from typing import Any, Dict, Optional


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
            self._api_requests_total = 0
            self._api_requests_by_method: Dict[str, int] = {}
            self._api_errors_total = 0
            self._api_errors_by_status: Dict[int, int] = {}
            self._auth_failures_total = 0
            self._rate_limit_rejections_total = 0

            # Simulation Metrics
            self._simulations_accepted = 0
            self._simulations_accepted_by_preset: Dict[str, int] = {}
            self._simulations_completed = 0
            self._simulations_failed = 0
            self._simulations_failed_by_code: Dict[str, int] = {}

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
    ) -> None:
        """Record an incoming HTTP request and response outcome."""
        with self._lock:
            self._api_requests_total += 1
            meth = method.upper()
            self._api_requests_by_method[meth] = self._api_requests_by_method.get(meth, 0) + 1

            if status_code >= 400:
                self._api_errors_total += 1
                self._api_errors_by_status[status_code] = (
                    self._api_errors_by_status.get(status_code, 0) + 1
                )

            if status_code in (401, 403):
                self._auth_failures_total += 1
            elif status_code == 429:
                self._rate_limit_rejections_total += 1

    def record_simulation_accepted(self, preset: str, num_interactions: int) -> None:
        """Record acceptance of a new simulation job."""
        with self._lock:
            self._simulations_accepted += 1
            self._simulations_accepted_by_preset[preset] = (
                self._simulations_accepted_by_preset.get(preset, 0) + 1
            )

    def record_simulation_completed(
        self,
        compute_ms: int,
        queue_wait_ms: Optional[int] = None,
    ) -> None:
        """Record successful worker completion of a simulation job."""
        with self._lock:
            self._simulations_completed += 1

            # Compute execution duration distribution
            if compute_ms is not None and compute_ms >= 0:
                self._sim_duration_count += 1
                self._sim_duration_total_ms += compute_ms
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
            uptime_seconds = int(time.time() - self._started_at)

            # Compute average latencies safely
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

            return {
                "uptime_seconds": uptime_seconds,
                "api": {
                    "requests_total": self._api_requests_total,
                    "requests_by_method": dict(self._api_requests_by_method),
                    "errors_total": self._api_errors_total,
                    "errors_by_status": dict(self._api_errors_by_status),
                    "auth_failures_total": self._auth_failures_total,
                    "rate_limit_rejections_total": self._rate_limit_rejections_total,
                },
                "simulations": {
                    "accepted": self._simulations_accepted,
                    "accepted_by_preset": dict(self._simulations_accepted_by_preset),
                    "completed": self._simulations_completed,
                    "failed": self._simulations_failed,
                    "failed_by_code": dict(self._simulations_failed_by_code),
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
                },
            }


# Singleton operational metrics instance
operational_metrics = OperationalMetrics()

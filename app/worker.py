"""Dedicated simulation worker process for asynchronous job execution."""

import argparse
import logging
import signal
import sys
import time
import uuid
from typing import Callable, Optional

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_engine, get_session_factory
from app.services.worker import claim_next_job, process_claimed_job

logger = logging.getLogger("behaviorsim_api.worker")


class SimulationWorker:
    """Autonomous worker that polls, claims, executes, and finalizes simulation jobs."""

    def __init__(
        self,
        worker_id: Optional[str] = None,
        poll_interval: float = 1.0,
        lease_timeout: int = 300,
        session_factory: Optional[sessionmaker[Session]] = None,
    ) -> None:
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval = poll_interval
        self.lease_timeout = lease_timeout
        self.session_factory = session_factory or get_session_factory()
        self._stop_requested = False

    def request_stop(self, *args, **kwargs) -> None:
        """Signal the worker loop to shut down cleanly after finishing the current job."""
        logger.info("Shutdown signal received for %s. Stopping after current iteration...", self.worker_id)
        self._stop_requested = True

    def run_once(self) -> bool:
        """Attempt to claim and process a single simulation job.

        Returns:
            True if a job was found and processed (success or handled failure),
            False if no eligible jobs were available in the queue.
        """
        with self.session_factory() as session:
            job = claim_next_job(
                db=session,
                worker_id=self.worker_id,
                lease_timeout_seconds=self.lease_timeout,
            )

        if job is None:
            return False

        logger.info("Worker %s processing job id=%s (preset=%s)", self.worker_id, job.id, job.preset)

        with self.session_factory() as session:
            process_claimed_job(
                db=session,
                job=job,
                worker_id=self.worker_id,
            )

        return True

    def run(self) -> None:
        """Main execution loop polling for jobs until termination signal is caught."""
        logger.info(
            "Starting SimulationWorker %s (poll_interval=%ss, lease_timeout=%ss)",
            self.worker_id,
            self.poll_interval,
            self.lease_timeout,
        )

        # Register termination handlers
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        try:
            while not self._stop_requested:
                did_work = self.run_once()
                if not did_work and not self._stop_requested:
                    time.sleep(self.poll_interval)
        finally:
            logger.info("SimulationWorker %s stopped cleanly.", self.worker_id)


def main() -> None:
    """CLI entrypoint for running the worker process."""
    parser = argparse.ArgumentParser(description="BehaviorSim Background Simulation Worker")
    parser.add_argument("--worker-id", type=str, default=None, help="Unique worker identifier")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval in seconds when queue is empty")
    parser.add_argument("--lease-timeout", type=int, default=300, help="Lease timeout in seconds before considering a running job stuck")

    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)

    worker = SimulationWorker(
        worker_id=args.worker_id,
        poll_interval=args.poll_interval,
        lease_timeout=args.lease_timeout,
    )
    worker.run()


if __name__ == "__main__":
    main()

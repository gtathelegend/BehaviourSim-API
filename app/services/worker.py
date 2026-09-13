"""Worker execution service managing atomic job claiming, background processing, and stuck-job recovery."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import BehaviorSimAPIError
from app.db.models.plan import Plan
from app.db.models.simulation import Simulation
from app.db.models.user import User
from app.services.simulation import (
    execute_simulation,
    normalize_preset_name,
)
from app.services.simulation_job import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    _get_execute_simulation,
    transition_job_status,
    utc_now,
)
from app.services.usage import (
    record_usage_result,
    refund_usage,
)

logger = logging.getLogger("behaviorsim_api.services.worker")


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware UTC, normalizing naive datetimes from SQLite."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)



def get_user_concurrency_capacity(
    db: Session,
    user_id: uuid.UUID,
    current_job_id: Optional[uuid.UUID] = None,
) -> bool:
    """Return True if user has capacity to run another concurrent simulation under their plan.

    Acquires an exclusive row lock on the user record to serialize concurrency checks
    across multiple distributed worker processes.
    """
    # 1. Lock user row to serialize concurrency check for this user
    user = db.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one_or_none()

    if user is None:
        return False

    # 2. Resolve plan concurrency limit
    max_concurrent = 1
    if user.plan_id:
        plan = db.get(Plan, user.plan_id)
        if plan and plan.max_concurrent_simulations:
            max_concurrent = plan.max_concurrent_simulations

    # 3. Count active running simulations for this user
    count_stmt = select(func.count(Simulation.id)).where(
        Simulation.user_id == user_id,
        Simulation.status == STATUS_RUNNING,
    )
    if current_job_id is not None:
        count_stmt = count_stmt.where(Simulation.id != current_job_id)

    active_count = db.execute(count_stmt).scalar() or 0
    return active_count < max_concurrent


from app.core.metrics import operational_metrics


def claim_next_job(
    db: Session,
    worker_id: str,
    lease_timeout_seconds: int = 300,
    max_pending_seconds: int = 3600,
) -> Optional[Simulation]:
    """Atomically claim the next eligible simulation job using PostgreSQL SKIP LOCKED.

    Guarantees:
    - Multiple concurrent workers never block each other or claim the same job.
    - Inspects pending jobs and expired running jobs (stuck-job recovery).
    - Checks user's max_concurrent_simulations before claiming.
    - Automatically expires and refunds pending jobs older than max_pending_seconds.
    - Exceeded max retries triggers automatic terminal failure transition with quota refund.
    - Returns claimed Simulation entity in 'running' status, or None if no job available.
    """
    now = utc_now()
    stuck_threshold = now - timedelta(seconds=lease_timeout_seconds)

    # 1. Query for candidate jobs: pending jobs OR running jobs whose lease expired
    # Ordered deterministically by created_at ASC (FIFO)
    stmt = (
        select(Simulation)
        .where(
            or_(
                Simulation.status == STATUS_PENDING,
                (
                    (Simulation.status == STATUS_RUNNING)
                    & (
                        (Simulation.heartbeat_at.is_(None))
                        | (Simulation.heartbeat_at < stuck_threshold)
                    )
                ),
            )
        )
        .order_by(Simulation.created_at.asc(), Simulation.id.asc())
        .with_for_update(skip_locked=True)
        .limit(10)
    )

    candidates = db.execute(stmt).scalars().all()
    if not candidates:
        return None

    for job in candidates:
        user_id = job.user_id

        # Case A: Candidate is an abandoned/stuck running job
        if job.status == STATUS_RUNNING:
            if job.attempt_count >= job.max_attempts:
                logger.warning(
                    "Job id=%s exceeded max attempts (%s/%s). Marking terminal failed.",
                    job.id,
                    job.attempt_count,
                    job.max_attempts,
                    extra={"simulation_id": str(job.id), "user_id": str(user_id)},
                )
                # Fail job permanently and refund quota
                job.status = STATUS_FAILED
                job.error_code = "execution_timeout"
                job.error_message = f"Job abandoned or exceeded maximum retry attempts ({job.max_attempts})."
                job.completed_at = now
                job.updated_at = now
                db.add(job)
                refund_usage(
                    db=db,
                    user_id=user_id,
                    requested_interactions=job.num_interactions,
                    delta_requests=1,
                )
                record_usage_result(
                    db=db,
                    user_id=user_id,
                    event_type="simulation_failed",
                    success=False,
                    interaction_count=0,
                    compute_ms=0,
                )
                db.commit()
                operational_metrics.record_simulation_failed("execution_timeout")
                continue  # Inspect next candidate

            # Stuck job with attempts remaining: verify user concurrency before re-claiming
            if not get_user_concurrency_capacity(db, user_id, current_job_id=job.id):
                continue

            # Recover stuck job
            job.attempt_count += 1
            job.worker_id = worker_id
            job.claimed_at = now
            job.heartbeat_at = now
            job.updated_at = now
            db.add(job)
            db.commit()
            db.refresh(job)
            logger.info(
                "Worker %s recovered stuck job id=%s (attempt %s/%s)",
                worker_id,
                job.id,
                job.attempt_count,
                job.max_attempts,
                extra={"simulation_id": str(job.id), "user_id": str(user_id)},
            )
            return job

        # Case B: Candidate is a pending job
        if job.status == STATUS_PENDING:
            # Check if pending job has exceeded maximum queue wait timeout
            created_at_utc = ensure_utc(job.created_at)
            if created_at_utc and created_at_utc < (now - timedelta(seconds=max_pending_seconds)):
                logger.warning(
                    "Job id=%s exceeded maximum pending queue time (%ss). Marking terminal failed.",
                    job.id,
                    max_pending_seconds,
                    extra={"simulation_id": str(job.id), "user_id": str(user_id)},
                )
                job.status = STATUS_FAILED
                job.error_code = "queue_timeout"
                job.error_message = f"Simulation queued longer than maximum wait time ({max_pending_seconds}s)."
                job.completed_at = now
                job.updated_at = now
                db.add(job)
                refund_usage(
                    db=db,
                    user_id=user_id,
                    requested_interactions=job.num_interactions,
                    delta_requests=1,
                )
                record_usage_result(
                    db=db,
                    user_id=user_id,
                    event_type="simulation_failed",
                    success=False,
                    interaction_count=0,
                    compute_ms=0,
                )
                db.commit()
                operational_metrics.record_simulation_failed("queue_timeout")
                continue  # Inspect next candidate


            # Check if user has concurrency capacity
            if not get_user_concurrency_capacity(db, user_id):
                continue

            # Atomically transition pending -> running with worker claim
            job.attempt_count += 1
            job.status = STATUS_RUNNING
            job.worker_id = worker_id
            job.claimed_at = now
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            job.updated_at = now
            db.add(job)
            db.commit()
            db.refresh(job)
            logger.info(
                "Worker %s claimed job id=%s (attempt %s/%s)",
                worker_id,
                job.id,
                job.attempt_count,
                job.max_attempts,
                extra={"simulation_id": str(job.id), "user_id": str(user_id)},
            )
            return job

    return None



def update_job_heartbeat(
    db: Session,
    job_id: uuid.UUID,
    worker_id: str,
) -> bool:
    """Update heartbeat timestamp of an actively running job to prevent lease expiration."""
    now = utc_now()
    job = db.get(Simulation, job_id)
    if job and job.status == STATUS_RUNNING and job.worker_id == worker_id:
        job.heartbeat_at = now
        job.updated_at = now
        db.add(job)
        db.commit()
        return True
    return False


def process_claimed_job(
    db: Session,
    job: Simulation,
    worker_id: str,
) -> bool:
    """Execute a claimed job through completion or failure recovery.

    Invariant: Database transaction is NOT held open while executing simulation engine.
    """
    job_id = job.id
    user_id = job.user_id
    num_interactions = job.num_interactions
    preset = job.preset
    seed = job.seed
    profile = job.profile
    initial_state = job.initial_state

    # 1. Execute BehaviorSim simulation engine outside DB transaction
    records: Optional[List[Dict[str, Any]]] = None
    compute_ms: Optional[int] = None
    exec_error_code: Optional[str] = None
    exec_error_message: Optional[str] = None

    try:
        run_sim = _get_execute_simulation()
        _, records, compute_ms = run_sim(
            preset=preset,
            num_interactions=num_interactions,
            seed=seed,
            profile=profile,
            initial_state=initial_state,
        )
    except BehaviorSimAPIError as b_err:
        exec_error_code = b_err.details.get("code", "simulation_generation_failed")
        exec_error_message = b_err.message
    except Exception as exc:
        logger.error("Simulation execution error for job_id=%s: %s", job_id, exc, exc_info=True)
        exec_error_code = "simulation_generation_failed"
        exec_error_message = "Internal simulation generation failed."

    now = utc_now()

    # 2. In short transaction: check job validity and persist outcome
    # Re-fetch row in fresh transaction
    current_job = db.get(Simulation, job_id)
    if not current_job or current_job.status != STATUS_RUNNING or current_job.worker_id != worker_id:
        logger.warning(
            "Job id=%s was modified, cancelled, or reassigned during execution. Discarding results.",
            job_id,
        )
        return False

    # Calculate queue wait duration if timestamps are present
    queue_wait_ms: Optional[int] = None
    if current_job.claimed_at and current_job.created_at:
        claimed_utc = ensure_utc(current_job.claimed_at)
        created_utc = ensure_utc(current_job.created_at)
        if claimed_utc and created_utc:
            queue_wait_ms = max(
                0, int((claimed_utc - created_utc).total_seconds() * 1000)
            )


    # Handle failure outcome
    if exec_error_code is not None:
        current_job.status = STATUS_FAILED
        current_job.error_code = exec_error_code
        current_job.error_message = exec_error_message
        current_job.completed_at = now
        current_job.updated_at = now
        db.add(current_job)
        refund_usage(
            db=db,
            user_id=user_id,
            requested_interactions=num_interactions,
            delta_requests=1,
        )
        record_usage_result(
            db=db,
            user_id=user_id,
            event_type="simulation_failed",
            success=False,
            interaction_count=0,
            compute_ms=0,
        )
        db.commit()
        operational_metrics.record_simulation_failed(exec_error_code)
        logger.info(
            "Job id=%s marked failed with code=%s",
            job_id,
            exec_error_code,
            extra={"simulation_id": str(job_id), "user_id": str(user_id)},
        )
        return False

    # Handle success outcome
    try:
        current_job.status = STATUS_COMPLETED
        current_job.data = records
        current_job.compute_ms = compute_ms
        current_job.completed_at = now
        current_job.updated_at = now
        db.add(current_job)
        record_usage_result(
            db=db,
            user_id=user_id,
            event_type="simulation_completed",
            success=True,
            interaction_count=num_interactions,
            compute_ms=compute_ms,
        )
        db.commit()
        operational_metrics.record_simulation_completed(
            compute_ms=compute_ms or 0,
            queue_wait_ms=queue_wait_ms,
        )
        logger.info(
            "Job id=%s completed successfully in %sms (queue_wait=%sms)",
            job_id,
            compute_ms,
            queue_wait_ms,
            extra={"simulation_id": str(job_id), "user_id": str(user_id)},
        )
        return True
    except Exception as exc:
        logger.error(
            "Failed to persist completed job id=%s: %s",
            job_id,
            exc,
            exc_info=True,
            extra={"simulation_id": str(job_id), "user_id": str(user_id)},
        )
        db.rollback()
        # Fallback: mark failed and refund quota
        try:
            current_job = db.get(Simulation, job_id)
            if current_job:
                current_job.status = STATUS_FAILED
                current_job.error_code = "simulation_persistence_failed"
                current_job.error_message = "Failed to persist simulation run."
                current_job.completed_at = now
                current_job.updated_at = now
                db.add(current_job)
                refund_usage(
                    db=db,
                    user_id=user_id,
                    requested_interactions=num_interactions,
                    delta_requests=1,
                )
                record_usage_result(
                    db=db,
                    user_id=user_id,
                    event_type="simulation_failed",
                    success=False,
                    interaction_count=0,
                    compute_ms=0,
                )
                db.commit()
                operational_metrics.record_simulation_failed("simulation_persistence_failed")
        except Exception:
            pass
        return False


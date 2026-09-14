"""Simulation data lifecycle and retention service for automated database cleanup."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Union
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.metrics import operational_metrics
from app.db.models.simulation import Simulation
from app.db.session import get_session_factory
from app.services.simulation_job import STATUS_COMPLETED, STATUS_FAILED

logger = logging.getLogger("behaviorsim_api.retention")

ELIGIBLE_RETENTION_STATUSES = [STATUS_COMPLETED, STATUS_FAILED]


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure datetime is timezone-aware UTC, normalizing naive datetimes from SQLite."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_now() -> datetime:
    """Return current UTC datetime with timezone awareness."""
    return datetime.now(timezone.utc)


@dataclass
class CleanupResult:
    """Operational summary of a simulation retention cleanup execution."""

    eligible_count: int
    deleted_count: int
    batch_count: int
    duration_ms: float
    cutoff: datetime
    dry_run: bool
    success: bool
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert result to dictionary for logging and API serialization."""
        return {
            "eligible_count": self.eligible_count,
            "deleted_count": self.deleted_count,
            "batch_count": self.batch_count,
            "duration_ms": round(self.duration_ms, 2),
            "cutoff": self.cutoff.isoformat(),
            "dry_run": self.dry_run,
            "success": self.success,
            "error_message": self.error_message,
        }


def get_retention_cutoff(retention_days: int) -> datetime:
    """Calculate the UTC timestamp cutoff prior to which completed/failed runs expire."""
    return utc_now() - timedelta(days=retention_days)


def count_eligible_simulations(
    db: Session,
    cutoff: datetime,
) -> int:
    """Count number of simulations eligible for retention cleanup without mutating data.

    Invariants:
    - Status MUST be in ('completed', 'failed').
    - Status 'pending' and 'running' are STRICTLY excluded and never counted.
    """
    stmt = select(func.count(Simulation.id)).where(
        Simulation.status.in_(ELIGIBLE_RETENTION_STATUSES),
        Simulation.created_at < cutoff,
    )
    return db.execute(stmt).scalar() or 0


def cleanup_expired_simulations(
    db: Optional[Session] = None,
    session_factory: Optional[Union[sessionmaker[Session], Callable[[], Session]]] = None,
    retention_days: Optional[int] = None,
    batch_size: Optional[int] = None,
    dry_run: bool = False,
    cutoff_override: Optional[datetime] = None,
) -> CleanupResult:
    """Execute bounded batch cleanup of expired completed and failed simulations.

    Semantics and Invariants:
    1. Eligibility: Only simulations with status in ('completed', 'failed') AND
       created_at < cutoff are eligible. 'pending' and 'running' are never touched.
    2. Quota Protection: This operation NEVER alters, deducts, or refunds user quota.
       Resource quota tracks historical usage incurred at simulation creation.
    3. Memory Safety: Only primary key UUIDs are queried into memory. Massive result
       JSON/JSONB payloads (data column) are deleted directly in the database without
       ever being loaded into application memory.
    4. Bounded Transactions: Deletions are executed in bounded batches. Each batch
       commits independently to keep transaction durations and database locks short.
    5. Concurrency Safety: On PostgreSQL, candidate IDs are queried with SKIP LOCKED
       to allow multiple cleanup workers to execute concurrently without lock contention.
    6. Idempotency: Repeating cleanup multiple times is safe; subsequent runs simply find
       0 remaining eligible rows.
    """
    settings = get_settings()
    actual_retention_days = retention_days if retention_days is not None else settings.SIMULATION_RETENTION_DAYS
    actual_batch_size = batch_size if batch_size is not None else settings.SIMULATION_CLEANUP_BATCH_SIZE

    if actual_retention_days < 1:
        raise ValueError(f"retention_days must be >= 1, got {actual_retention_days}")
    if actual_batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {actual_batch_size}")

    cutoff = ensure_utc(cutoff_override) if cutoff_override else get_retention_cutoff(actual_retention_days)

    start_time = time.perf_counter()
    factory = session_factory or (get_session_factory() if db is None else None)

    # Dry-run execution
    if dry_run:
        try:
            if db is not None:
                eligible = count_eligible_simulations(db, cutoff)
            else:
                assert factory is not None
                with factory() as session:
                    eligible = count_eligible_simulations(session, cutoff)

            duration_ms = (time.perf_counter() - start_time) * 1000.0
            logger.info(
                "Simulation retention cleanup dry-run: %s eligible simulations (cutoff=%s)",
                eligible,
                cutoff.isoformat(),
                extra={
                    "operation": "simulation_cleanup",
                    "eligible_count": eligible,
                    "deleted_count": 0,
                    "batch_count": 0,
                    "duration_ms": duration_ms,
                    "cutoff": cutoff.isoformat(),
                    "dry_run": True,
                },
            )
            return CleanupResult(
                eligible_count=eligible,
                deleted_count=0,
                batch_count=0,
                duration_ms=duration_ms,
                cutoff=cutoff,
                dry_run=True,
                success=True,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error("Simulation retention cleanup dry-run failed: %s", exc, exc_info=True)
            return CleanupResult(
                eligible_count=0,
                deleted_count=0,
                batch_count=0,
                duration_ms=duration_ms,
                cutoff=cutoff,
                dry_run=True,
                success=False,
                error_message=str(exc),
            )

    # Live mutation execution
    deleted_total = 0
    batch_count = 0
    eligible_count = 0

    try:
        # Determine initial eligible count
        if db is not None:
            eligible_count = count_eligible_simulations(db, cutoff)
        else:
            assert factory is not None
            with factory() as session:
                eligible_count = count_eligible_simulations(session, cutoff)

        if eligible_count == 0:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            operational_metrics.record_cleanup_run(deleted_count=0, duration_ms=duration_ms, success=True)
            logger.info(
                "Simulation retention cleanup: 0 eligible simulations found. Exiting.",
                extra={
                    "operation": "simulation_cleanup",
                    "eligible_count": 0,
                    "deleted_count": 0,
                    "batch_count": 0,
                    "duration_ms": duration_ms,
                    "cutoff": cutoff.isoformat(),
                    "dry_run": False,
                },
            )
            return CleanupResult(
                eligible_count=0,
                deleted_count=0,
                batch_count=0,
                duration_ms=duration_ms,
                cutoff=cutoff,
                dry_run=False,
                success=True,
            )

        # Batch deletion loop
        while True:
            deleted_in_batch, candidates_found = _delete_single_batch(
                db=db,
                session_factory=factory,
                cutoff=cutoff,
                batch_size=actual_batch_size,
            )
            if candidates_found == 0:
                break

            deleted_total += deleted_in_batch
            if deleted_in_batch > 0:
                batch_count += 1

        duration_ms = (time.perf_counter() - start_time) * 1000.0
        operational_metrics.record_cleanup_run(deleted_count=deleted_total, duration_ms=duration_ms, success=True)

        logger.info(
            "Simulation retention cleanup completed: deleted=%s, batches=%s, duration_ms=%s",
            deleted_total,
            batch_count,
            round(duration_ms, 2),
            extra={
                "operation": "simulation_cleanup",
                "eligible_count": eligible_count,
                "deleted_count": deleted_total,
                "batch_count": batch_count,
                "duration_ms": duration_ms,
                "cutoff": cutoff.isoformat(),
                "dry_run": False,
            },
        )

        return CleanupResult(
            eligible_count=eligible_count,
            deleted_count=deleted_total,
            batch_count=batch_count,
            duration_ms=duration_ms,
            cutoff=cutoff,
            dry_run=False,
            success=True,
        )

    except Exception as exc:
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        operational_metrics.record_cleanup_run(deleted_count=deleted_total, duration_ms=duration_ms, success=False)
        logger.error("Simulation retention cleanup encountered error: %s", exc, exc_info=True)
        return CleanupResult(
            eligible_count=eligible_count,
            deleted_count=deleted_total,
            batch_count=batch_count,
            duration_ms=duration_ms,
            cutoff=cutoff,
            dry_run=False,
            success=False,
            error_message=str(exc),
        )


def _delete_single_batch(
    db: Optional[Session],
    session_factory: Optional[Union[sessionmaker[Session], Callable[[], Session]]],
    cutoff: datetime,
    batch_size: int,
) -> tuple[int, int]:
    """Execute deletion of a single bounded batch of eligible simulation IDs.

    Uses short transaction, commits immediately, and rolls back on failure.
    Returns (actual_deleted_rows, candidates_found_count).
    """
    if db is not None:
        return _perform_batch_delete(db, cutoff, batch_size)

    assert session_factory is not None
    with session_factory() as session:
        return _perform_batch_delete(session, cutoff, batch_size)


def _perform_batch_delete(
    session: Session,
    cutoff: datetime,
    batch_size: int,
) -> tuple[int, int]:
    """Perform candidate ID selection and deletion within the given session."""
    try:
        # Build candidate ID query selecting ONLY Simulation.id (never loading JSON data into memory)
        stmt = (
            select(Simulation.id)
            .where(
                Simulation.status.in_(ELIGIBLE_RETENTION_STATUSES),
                Simulation.created_at < cutoff,
            )
            .order_by(Simulation.created_at.asc(), Simulation.id.asc())
            .limit(batch_size)
        )

        # Apply row-level locking with SKIP LOCKED on PostgreSQL to prevent worker collisions
        bind = session.get_bind()
        if bind is not None and getattr(bind.dialect, "name", "") == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        candidate_ids = session.execute(stmt).scalars().all()
        if not candidate_ids:
            return 0, 0

        # Execute bulk delete by ID
        del_stmt = delete(Simulation).where(Simulation.id.in_(candidate_ids))
        res = session.execute(del_stmt)
        session.commit()

        actual_deleted = res.rowcount if (res.rowcount is not None and res.rowcount >= 0) else len(candidate_ids)
        return actual_deleted, len(candidate_ids)
    except Exception:
        session.rollback()
        raise

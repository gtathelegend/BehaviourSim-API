"""Operator CLI command for executing simulation data retention cleanup."""

import argparse
import logging
import sys

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import get_session_factory
from app.services.retention import cleanup_expired_simulations

logger = logging.getLogger("behaviorsim_api.cleanup")


def main() -> None:
    """CLI entrypoint for running simulation data retention cleanup."""
    parser = argparse.ArgumentParser(
        description="BehaviorSim Simulation Data Retention Cleanup CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Simulate cleanup by counting eligible expired simulations without deleting any rows.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="Retention threshold in days. Defaults to SIMULATION_RETENTION_DAYS from settings.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Batch size for deletions. Defaults to SIMULATION_CLEANUP_BATCH_SIZE from settings.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        help="Override logging level (e.g., INFO, DEBUG, WARNING).",
    )

    args = parser.parse_args()
    settings = get_settings()

    setup_logging(log_level=args.log_level or settings.LOG_LEVEL)

    retention_days = args.retention_days if args.retention_days is not None else settings.SIMULATION_RETENTION_DAYS
    batch_size = args.batch_size if args.batch_size is not None else settings.SIMULATION_CLEANUP_BATCH_SIZE

    if retention_days < 1:
        logger.error("Invalid retention_days: %d. Must be >= 1.", retention_days)
        print(f"ERROR: --retention-days must be a positive integer (>= 1), got {retention_days}", file=sys.stderr)
        sys.exit(2)

    if batch_size < 1 or batch_size > 1000:
        logger.error("Invalid batch_size: %d. Must be between 1 and 1000.", batch_size)
        print(f"ERROR: --batch-size must be between 1 and 1000, got {batch_size}", file=sys.stderr)
        sys.exit(2)

    print("=" * 65)
    print("      BehaviorSim Simulation Data Retention Cleanup")
    print("=" * 65)
    print(f"Mode:            {'DRY RUN (No changes)' if args.dry_run else 'LIVE MUTATION'}")
    print(f"Retention Days:  {retention_days} days")
    print(f"Batch Size:      {batch_size} rows / transaction")
    print("-" * 65)

    try:
        session_factory = get_session_factory()
        result = cleanup_expired_simulations(
            session_factory=session_factory,
            retention_days=retention_days,
            batch_size=batch_size,
            dry_run=args.dry_run,
        )

        if not result.success:
            print(f"ERROR: Cleanup failed: {result.error_message}")
            sys.exit(1)

        print("Execution Results:")
        print(f"  Cutoff (UTC):     {result.cutoff.isoformat()}")
        print(f"  Eligible Rows:    {result.eligible_count:,}")
        print(f"  Deleted Rows:     {result.deleted_count:,}")
        print(f"  Batches Executed: {result.batch_count:,}")
        print(f"  Duration:         {result.duration_ms:.2f} ms")
        print("=" * 65)
        sys.exit(0)
    except Exception as exc:
        logger.error("Retention cleanup CLI failed: %s", exc, exc_info=True)
        print(f"FATAL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

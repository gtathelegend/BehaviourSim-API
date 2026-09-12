"""Safe database connectivity verification script for BehaviorSim API.

Tests connectivity to the configured database by executing `SELECT 1`.
Sanitizes output so that database credentials and secrets are never printed.
"""

import os
import sys
from pathlib import Path

# Ensure repository root is on sys.path for direct script execution
repo_root = str(Path(__file__).resolve().parent.parent)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from sqlalchemy import text


def main() -> None:
    from app.core.config import get_settings
    from app.db.session import get_engine

    settings = get_settings()
    # Mask database URL for safe logging: show scheme and host only
    db_url = settings.DATABASE_URL
    masked_target = "database"
    if "@" in db_url:
        host_part = db_url.split("@")[-1].split("/")[0]
        masked_target = f"host '{host_part}'"
    elif "sqlite" in db_url:
        masked_target = "SQLite"

    print(f"Connecting to {masked_target}...")

    try:
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar()
            if result == 1:
                print("SUCCESS: Database connectivity verified (SELECT 1 returned 1).")
                sys.exit(0)
            else:
                print(f"FAILED: Unexpected query result: {result}")
                sys.exit(1)
    except Exception as exc:
        print(f"FAILED: Database connection error: {type(exc).__name__}")
        sys.exit(1)


if __name__ == "__main__":
    main()

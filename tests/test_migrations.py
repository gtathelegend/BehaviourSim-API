"""Tests verifying Alembic database migration execution."""

from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_downgrade_cycle(tmp_path: Path):
    """Verify alembic upgrade, downgrade, and re-upgrade cycle against an isolated test database."""
    test_db_file = tmp_path / "test_migrations.db"
    sqlite_url = f"sqlite:///{test_db_file.as_posix()}"

    ini_path = Path(__file__).resolve().parent.parent / "alembic.ini"
    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", sqlite_url)

    # 1. Upgrade to head
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(sqlite_url)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "users" in tables
    assert "auth_identities" in tables
    assert "api_keys" in tables
    assert "user_sessions" in tables
    assert "plans" in tables
    assert "monthly_usage" in tables
    assert "usage_events" in tables
    assert "alembic_version" in tables

    # 2. Downgrade to base
    command.downgrade(alembic_cfg, "base")
    inspector = inspect(engine)
    tables_after_downgrade = inspector.get_table_names()
    assert "users" not in tables_after_downgrade
    assert "auth_identities" not in tables_after_downgrade
    assert "api_keys" not in tables_after_downgrade
    assert "user_sessions" not in tables_after_downgrade
    assert "plans" not in tables_after_downgrade
    assert "monthly_usage" not in tables_after_downgrade
    assert "usage_events" not in tables_after_downgrade

    # 3. Re-upgrade to head
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    re_tables = inspector.get_table_names()
    assert "users" in re_tables
    assert "auth_identities" in re_tables
    assert "api_keys" in re_tables
    assert "user_sessions" in re_tables
    assert "plans" in re_tables
    assert "monthly_usage" in re_tables
    assert "usage_events" in re_tables

"""Add plans, monthly_usage, and usage_events tables

Revision ID: 0003_plans_and_usage
Revises: 0002_user_sessions
Create Date: 2026-09-12 23:25:00.000000

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_plans_and_usage"
down_revision: Union[str, None] = "0002_user_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FREE_PLAN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def upgrade() -> None:
    # 1. Create plans table
    plans_table = op.create_table(
        "plans",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("monthly_requests", sa.Integer(), nullable=False),
        sa.Column("monthly_interactions", sa.Integer(), nullable=False),
        sa.Column("max_interactions_per_request", sa.Integer(), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_simulations", sa.Integer(), nullable=False),
        sa.Column("max_api_keys", sa.Integer(), nullable=False),
        sa.Column("calibration_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("large_exports_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
    )
    op.create_index("ix_plans_name", "plans", ["name"], unique=True)

    # 2. Seed default 'free' plan
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        plans_table,
        [
            {
                "id": FREE_PLAN_ID,
                "name": "free",
                "monthly_requests": 100,
                "monthly_interactions": 10000,
                "max_interactions_per_request": 1000,
                "requests_per_minute": 5,
                "max_concurrent_simulations": 1,
                "max_api_keys": 1,
                "calibration_enabled": False,
                "large_exports_enabled": False,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )

    # 3. Add plan_id to users with batch_alter_table for SQLite compatibility
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "plan_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("plans.id", name="fk_users_plan_id_plans"),
                nullable=False,
                server_default=str(FREE_PLAN_ID),
            )
        )

    # 4. Create monthly_usage table
    op.create_table(
        "monthly_usage",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_monthly_usage_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_monthly_usage"),
        sa.UniqueConstraint("user_id", "period_start", name="uq_monthly_usage_user_period"),
    )
    op.create_index("ix_monthly_usage_user_id", "monthly_usage", ["user_id"])
    op.create_index("ix_monthly_usage_period_start", "monthly_usage", ["period_start"])

    # 5. Create usage_events table
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("api_key_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("interaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compute_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_usage_events_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name="fk_usage_events_api_key_id_api_keys",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
    )
    op.create_index("ix_usage_events_user_id", "usage_events", ["user_id"])
    op.create_index("ix_usage_events_api_key_id", "usage_events", ["api_key_id"])
    op.create_index("ix_usage_events_event_type", "usage_events", ["event_type"])
    op.create_index("ix_usage_events_request_id", "usage_events", ["request_id"])
    op.create_index("ix_usage_events_created_at", "usage_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_created_at", table_name="usage_events")
    op.drop_index("ix_usage_events_request_id", table_name="usage_events")
    op.drop_index("ix_usage_events_event_type", table_name="usage_events")
    op.drop_index("ix_usage_events_api_key_id", table_name="usage_events")
    op.drop_index("ix_usage_events_user_id", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("ix_monthly_usage_period_start", table_name="monthly_usage")
    op.drop_index("ix_monthly_usage_user_id", table_name="monthly_usage")
    op.drop_table("monthly_usage")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("plan_id")

    op.drop_index("ix_plans_name", table_name="plans")
    op.drop_table("plans")

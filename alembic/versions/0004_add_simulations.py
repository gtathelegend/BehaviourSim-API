"""Add simulations table for durable run metadata and result persistence

Revision ID: 0004_add_simulations
Revises: 0003_plans_and_usage
Create Date: 2026-09-14 01:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0004_add_simulations"
down_revision: Union[str, None] = "0003_plans_and_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create simulations table
    json_type = sa.JSON().with_variant(JSONB, "postgresql")
    op.create_table(
        "simulations",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("preset", sa.String(length=50), nullable=False),
        sa.Column("num_interactions", sa.Integer(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("profile", sa.String(length=50), nullable=True),
        sa.Column("initial_state", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="completed"),
        sa.Column("result_storage", sa.String(length=20), nullable=False, server_default="database"),
        sa.Column("result_location", sa.String(length=255), nullable=True),
        sa.Column("data", json_type, nullable=False),
        sa.Column("behaviorsim_version", sa.String(length=20), nullable=False),
        sa.Column("api_version", sa.String(length=20), nullable=False),
        sa.Column("compute_ms", sa.Integer(), nullable=False),
        sa.Column("reproducible", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_simulations_user_id", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_simulations"),
    )
    op.create_index("ix_simulations_user_id", "simulations", ["user_id"], unique=False)
    op.create_index("ix_simulations_created_at", "simulations", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_simulations_created_at", table_name="simulations")
    op.drop_index("ix_simulations_user_id", table_name="simulations")
    op.drop_table("simulations")

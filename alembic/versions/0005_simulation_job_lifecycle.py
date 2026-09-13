"""Add simulation job lifecycle fields, index, and nullable result columns

Revision ID: 0005_simulation_job_lifecycle
Revises: 0004_add_simulations
Create Date: 2026-09-14 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0005_simulation_job_lifecycle"
down_revision: Union[str, None] = "0004_add_simulations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(JSONB, "postgresql")

    with op.batch_alter_table("simulations") as batch_op:
        # 1. Add job lifecycle timestamps and failure metadata columns
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
        )
        batch_op.add_column(sa.Column("error_code", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("error_message", sa.String(length=255), nullable=True))

        # 2. Make result and completion fields nullable for pending/running/failed states
        batch_op.alter_column("data", existing_type=json_type, nullable=True)
        batch_op.alter_column("compute_ms", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("completed_at", existing_type=sa.DateTime(timezone=True), nullable=True)

        # 3. Add status index for efficient filtering
        batch_op.create_index("ix_simulations_status", ["status"], unique=False)


def downgrade() -> None:
    json_type = sa.JSON().with_variant(JSONB, "postgresql")

    with op.batch_alter_table("simulations") as batch_op:
        batch_op.drop_index("ix_simulations_status")

        batch_op.alter_column("completed_at", existing_type=sa.DateTime(timezone=True), nullable=False)
        batch_op.alter_column("compute_ms", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("data", existing_type=json_type, nullable=False)

        batch_op.drop_column("error_message")
        batch_op.drop_column("error_code")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("started_at")

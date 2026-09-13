"""Add worker execution columns, retry metadata, and queue index

Revision ID: 0006_worker_job_execution
Revises: 0005_simulation_job_lifecycle
Create Date: 2026-09-14 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0006_worker_job_execution"
down_revision: Union[str, None] = "0005_simulation_job_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("simulations") as batch_op:
        # 1. Add worker tracking and lease heartbeat fields
        batch_op.add_column(sa.Column("worker_id", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))

        # 2. Add retry bounds
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
        )

        # 3. Add compound index for fast SKIP LOCKED queue polling
        batch_op.create_index("ix_simulations_queue", ["status", "created_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("simulations") as batch_op:
        batch_op.drop_index("ix_simulations_queue")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("worker_id")

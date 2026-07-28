"""add durable dual-write failure records

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dual_write_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("error_type", sa.String(length=120), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("idempotency_key", name="uq_dual_write_failures_idempotency_key"),
    )
    op.create_index(
        "idx_dual_write_failures_open",
        "dual_write_failures",
        ["client_id", "resolved_at", "last_failed_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_dual_write_failures_open", table_name="dual_write_failures")
    op.drop_table("dual_write_failures")

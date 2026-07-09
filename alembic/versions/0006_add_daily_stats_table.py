"""add daily_stats table for analytics rollups

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "daily_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("stats", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("client_id", "date", name="uq_daily_stats_client_date"),
    )
    op.create_index("idx_daily_stats_client_date", "daily_stats", ["client_id", "date"])


def downgrade() -> None:
    op.drop_index("idx_daily_stats_client_date", table_name="daily_stats")
    op.drop_table("daily_stats")

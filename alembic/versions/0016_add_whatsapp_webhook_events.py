"""add durable WhatsApp webhook event receipts

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_kind", sa.String(length=20), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("phone_number_id", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="received"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rq_job_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", "event_kind", "event_id", name="uq_whatsapp_webhook_event"),
    )
    op.create_index("idx_whatsapp_webhook_events_state", "whatsapp_webhook_events", ["state", "received_at"])
    op.create_index("ix_whatsapp_webhook_events_correlation_id", "whatsapp_webhook_events", ["correlation_id"])
    op.create_index("ix_whatsapp_webhook_events_rq_job_id", "whatsapp_webhook_events", ["rq_job_id"])


def downgrade() -> None:
    op.drop_table("whatsapp_webhook_events")

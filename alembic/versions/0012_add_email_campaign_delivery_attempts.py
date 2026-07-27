"""add durable email campaign delivery attempts

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "email_campaign_enrollments",
        sa.Column("delivery_run_id", sa.String(length=64), nullable=True),
    )
    op.execute(
        """
        UPDATE email_campaign_enrollments
        SET delivery_run_id = 'legacy-' || id::text || '-' ||
            EXTRACT(EPOCH FROM COALESCE(enrolled_at, CURRENT_TIMESTAMP))::bigint::text
        WHERE delivery_run_id IS NULL
        """
    )
    op.alter_column(
        "email_campaign_enrollments",
        "delivery_run_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )

    op.create_table(
        "email_campaign_delivery_attempts",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("enrollment_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("delivery_run_id", sa.String(length=64), nullable=False),
        sa.Column("step_position", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'sending', 'sent', 'failed')",
            name="ck_email_delivery_attempt_state",
        ),
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["email_campaign_enrollments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["email_campaigns.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "campaign_id",
            "enrollment_id",
            "delivery_run_id",
            "step_position",
            name="uq_email_delivery_attempt_execution",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_email_delivery_attempt_idempotency_key",
        ),
    )
    op.create_index(
        "idx_email_delivery_attempt_enrollment",
        "email_campaign_delivery_attempts",
        ["enrollment_id", "delivery_run_id"],
        unique=False,
    )
    op.create_index(
        "idx_email_delivery_attempt_client",
        "email_campaign_delivery_attempts",
        ["client_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_email_delivery_attempt_client",
        table_name="email_campaign_delivery_attempts",
    )
    op.drop_index(
        "idx_email_delivery_attempt_enrollment",
        table_name="email_campaign_delivery_attempts",
    )
    op.drop_table("email_campaign_delivery_attempts")
    op.drop_column("email_campaign_enrollments", "delivery_run_id")

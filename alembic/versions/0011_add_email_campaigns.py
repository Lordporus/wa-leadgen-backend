"""add email campaigns / sequences (Phase E7)

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-18

Tables:
  - email_campaigns
  - email_campaign_steps
  - email_campaign_enrollments

GENERATE-ONLY — apply with stamp-awareness on production.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_campaigns",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_email_campaigns_client",
        "email_campaigns",
        ["client_id"],
        unique=False,
    )

    op.create_table(
        "email_campaign_steps",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "delay_hours",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("subject_template", sa.String(length=500), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["email_campaigns.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "campaign_id", "position", name="uq_email_campaign_steps_pos"
        ),
    )
    op.create_index(
        "idx_email_campaign_steps_campaign",
        "email_campaign_steps",
        ["campaign_id"],
        unique=False,
    )

    op.create_table(
        "email_campaign_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("lead_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "current_step",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(length=40), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["email_campaigns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "campaign_id",
            "lead_id",
            name="uq_email_campaign_enrollments_campaign_lead",
        ),
    )
    op.create_index(
        "idx_email_enrollments_due",
        "email_campaign_enrollments",
        ["status", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "idx_email_enrollments_client",
        "email_campaign_enrollments",
        ["client_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_email_enrollments_client", table_name="email_campaign_enrollments")
    op.drop_index("idx_email_enrollments_due", table_name="email_campaign_enrollments")
    op.drop_table("email_campaign_enrollments")
    op.drop_index(
        "idx_email_campaign_steps_campaign", table_name="email_campaign_steps"
    )
    op.drop_table("email_campaign_steps")
    op.drop_index("idx_email_campaigns_client", table_name="email_campaigns")
    op.drop_table("email_campaigns")

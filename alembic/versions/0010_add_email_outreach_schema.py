"""add email outreach schema (Phase E1)

Revision ID: 0010
Revises: 7fa54922a7af
Create Date: 2026-07-18

Phase E1 only — schema for the email channel. No send API / webhooks yet.

Adds:
  - clients: email_enabled, email_provider, from/reply/footer fields,
    email_api_key_encrypted (reserved for BYOK; do not store plaintext)
  - leads: email, email_status, email_opt_in_at, email_opt_in_source
  - messages: channel, subject, provider_message_id, thread_id,
    email_headers, provider_metadata
  - email_suppressions table (tenant-scoped do-not-email list)
  - partial unique index uq_leads_client_email WHERE email IS NOT NULL

GENERATE-ONLY policy: do NOT apply to production without alembic stamp
awareness (same as 0004–0009). Existing prod may already be past 0008;
confirm `alembic_version` before upgrade.

Phone remains required on leads (email is optional).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0010"
down_revision: Union[str, None] = "7fa54922a7af"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── clients: per-tenant email settings ────────────────────────────────
    op.add_column(
        "clients",
        sa.Column(
            "email_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "clients",
        sa.Column(
            "email_provider",
            sa.String(length=30),
            nullable=False,
            server_default="resend",
        ),
    )
    op.add_column(
        "clients",
        sa.Column("email_from_address", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("email_from_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("email_reply_to", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("email_company_address", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("email_footer_html", sa.Text(), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("email_api_key_encrypted", sa.Text(), nullable=True),
    )

    # ── leads: optional email contact ─────────────────────────────────────
    op.add_column(
        "leads",
        sa.Column("email", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column("email_status", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column("email_opt_in_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "leads",
        sa.Column("email_opt_in_source", sa.String(length=100), nullable=True),
    )
    # Tenant-scoped uniqueness: many NULL emails allowed; non-null unique per client.
    op.create_index(
        "uq_leads_client_email",
        "leads",
        ["client_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(
        "idx_leads_client_email",
        "leads",
        ["client_id", "email"],
        unique=False,
    )

    # ── messages: multi-channel columns ───────────────────────────────────
    op.add_column(
        "messages",
        sa.Column(
            "channel",
            sa.String(length=20),
            nullable=False,
            server_default="whatsapp",
        ),
    )
    op.add_column(
        "messages",
        sa.Column("subject", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("thread_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column(
            "email_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "messages",
        sa.Column(
            "provider_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_messages_provider_message_id",
        "messages",
        ["provider_message_id"],
        unique=False,
    )
    op.create_index(
        "idx_messages_lead_channel",
        "messages",
        ["lead_id", "channel"],
        unique=False,
    )

    # ── email_suppressions ────────────────────────────────────────────────
    op.create_table(
        "email_suppressions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "client_id",
            "email",
            name="uq_email_suppressions_client_email",
        ),
    )
    op.create_index(
        "idx_email_suppressions_client_email",
        "email_suppressions",
        ["client_id", "email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_email_suppressions_client_email",
        table_name="email_suppressions",
    )
    op.drop_table("email_suppressions")

    op.drop_index("idx_messages_lead_channel", table_name="messages")
    op.drop_index("idx_messages_provider_message_id", table_name="messages")
    op.drop_column("messages", "provider_metadata")
    op.drop_column("messages", "email_headers")
    op.drop_column("messages", "thread_id")
    op.drop_column("messages", "provider_message_id")
    op.drop_column("messages", "subject")
    op.drop_column("messages", "channel")

    op.drop_index("idx_leads_client_email", table_name="leads")
    op.drop_index("uq_leads_client_email", table_name="leads")
    op.drop_column("leads", "email_opt_in_source")
    op.drop_column("leads", "email_opt_in_at")
    op.drop_column("leads", "email_status")
    op.drop_column("leads", "email")

    op.drop_column("clients", "email_api_key_encrypted")
    op.drop_column("clients", "email_footer_html")
    op.drop_column("clients", "email_company_address")
    op.drop_column("clients", "email_reply_to")
    op.drop_column("clients", "email_from_name")
    op.drop_column("clients", "email_from_address")
    op.drop_column("clients", "email_provider")
    op.drop_column("clients", "email_enabled")

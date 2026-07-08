"""initial schema from existing models

Revision ID: 0001
Revises:
Create Date: 2026-07-07
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- clients ---
    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("wa_phone_number_id", sa.String(50), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("followup_template", sa.String(100), nullable=True),
        sa.Column("calendly_link", sa.String(255), nullable=True),
        sa.Column("dashboard_api_key_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true")),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("brand_color", sa.String(20), server_default="'#C8A96E'", nullable=True),
        sa.Column("logo_url", sa.String(500), nullable=True),
        sa.Column("company_display_name", sa.String(255), nullable=True),
        sa.Column("admin_phone", sa.String(50), nullable=True),
        sa.Column("calendly_api_token", sa.String(255), nullable=True),
    )

    # --- leads ---
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(255), server_default="'WhatsApp User'"),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("status", sa.String(50), server_default="'New Lead'"),
        sa.Column("business_name", sa.String(255), nullable=True),
        sa.Column("lead_score", sa.String(20), nullable=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_leads_phone", "leads", ["phone"], unique=True)
    op.create_index("ix_leads_status", "leads", ["status"])

    # --- messages ---
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("msg_type", sa.String(20), server_default="'text'"),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("wa_message_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_messages_wa_message_id", "messages", ["wa_message_id"])
    op.create_index("idx_messages_lead_id", "messages", ["lead_id"])

    # --- pipeline_stages ---
    op.create_table(
        "pipeline_stages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_won", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("is_lost", sa.Boolean(), server_default=sa.text("false")),
    )

    # --- prompt_templates ---
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("niche", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("prompt_templates")
    op.drop_table("pipeline_stages")
    op.drop_index("idx_messages_lead_id", table_name="messages")
    op.drop_index("ix_messages_wa_message_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_leads_status", table_name="leads")
    op.drop_index("ix_leads_phone", table_name="leads")
    op.drop_table("leads")
    op.drop_table("clients")

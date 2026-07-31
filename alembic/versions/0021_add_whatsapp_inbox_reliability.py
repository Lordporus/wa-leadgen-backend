"""add versioned WhatsApp takeover and inbox records

Revision ID: 0021
Revises: 0020
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("takeover_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("leads", sa.Column("takeover_owner", sa.String(length=120), nullable=True))
    op.add_column("leads", sa.Column("takeover_reason", sa.String(length=255), nullable=True))
    op.add_column("leads", sa.Column("takeover_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("leads", sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "whatsapp_operator_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=80), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("from_version", sa.Integer(), nullable=True),
        sa.Column("to_version", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("outbound_intent_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", "idempotency_key", name="uq_whatsapp_operator_action_idempotency"),
    )
    op.create_index("idx_whatsapp_operator_action_lead_time", "whatsapp_operator_actions", ["client_id", "lead_id", "created_at"])
    op.create_table(
        "whatsapp_takeover_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("takeover_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="open"),
        sa.Column("owner", sa.String(length=120), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_whatsapp_takeover_queue", "whatsapp_takeover_tasks", ["client_id", "status", "created_at"])
    op.add_column("whatsapp_outbound_intents", sa.Column("intent_kind", sa.String(length=30), nullable=False, server_default="ai_reply"))
    op.add_column("whatsapp_outbound_intents", sa.Column("takeover_version", sa.Integer(), nullable=True))
    op.add_column("whatsapp_outbound_intents", sa.Column("operator_action_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_whatsapp_outbound_operator_action", "whatsapp_outbound_intents", "whatsapp_operator_actions", ["operator_action_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    raise RuntimeError("Phase 10 inbox reliability records are forward-only")

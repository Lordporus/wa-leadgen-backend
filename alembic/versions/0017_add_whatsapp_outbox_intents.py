"""add durable WhatsApp outbound intents

Revision ID: 0017
Revises: 0016
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_outbound_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inbound_event_id", sa.Integer(), sa.ForeignKey("whatsapp_webhook_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reply_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("recipient_phone", sa.String(length=50), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("provider_status", sa.String(length=30), nullable=True),
        sa.Column("failure_category", sa.String(length=50), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id", "inbound_event_id", "reply_version", name="uq_whatsapp_outbound_intent_reply"),
        sa.UniqueConstraint("client_id", "provider_message_id", name="uq_whatsapp_outbound_intent_provider_message"),
    )
    op.create_index("idx_whatsapp_outbound_intents_state", "whatsapp_outbound_intents", ["state", "created_at"])
    op.add_column("leads", sa.Column("whatsapp_opted_out_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("outbound_intent_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_messages_outbound_intent", "messages", "whatsapp_outbound_intents", ["outbound_intent_id"], ["id"], ondelete="SET NULL")
    op.create_unique_constraint("uq_messages_outbound_intent", "messages", ["outbound_intent_id"])


def downgrade() -> None:
    op.drop_constraint("uq_messages_outbound_intent", "messages", type_="unique")
    op.drop_constraint("fk_messages_outbound_intent", "messages", type_="foreignkey")
    op.drop_column("messages", "outbound_intent_id")
    op.drop_column("leads", "whatsapp_opted_out_at")
    op.drop_table("whatsapp_outbound_intents")

"""add controlled WhatsApp follow-up sequences

Revision ID: 0019
Revises: 0018
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table("whatsapp_sequences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table("whatsapp_sequence_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sequence_id", sa.Integer(), sa.ForeignKey("whatsapp_sequences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("delay_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("whatsapp_templates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.UniqueConstraint("sequence_id", "position", name="uq_whatsapp_sequence_step_position"),
    )
    op.create_table("whatsapp_sequence_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sequence_id", sa.Integer(), sa.ForeignKey("whatsapp_sequences.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("current_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(length=80), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sequence_id", "lead_id", name="uq_whatsapp_sequence_enrollment"),
    )
    op.create_index("idx_whatsapp_sequence_enrollment_due", "whatsapp_sequence_enrollments", ["status", "next_run_at"])
    op.create_index("idx_whatsapp_sequence_enrollment_client", "whatsapp_sequence_enrollments", ["client_id"])
    op.create_table("whatsapp_sequence_executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("whatsapp_sequence_enrollments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_position", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="sending"),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("enrollment_id", "step_position", "attempt_number", name="uq_whatsapp_sequence_execution_attempt"),
    )


def downgrade() -> None:
    raise RuntimeError("Phase 8 sequence state is forward-only")

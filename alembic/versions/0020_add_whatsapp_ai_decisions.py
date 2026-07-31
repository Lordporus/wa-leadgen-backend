"""add controlled WhatsApp AI decision records

Revision ID: 0020
Revises: 0019
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_ai_prompt_models",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("prompt_body", sa.Text(), nullable=False),
        sa.Column("model_route", sa.String(length=120), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.String(length=30), nullable=False),
        sa.Column("allowed_languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='["en"]'),
        sa.Column("tone", sa.String(length=50), nullable=False, server_default="professional"),
        sa.Column("evaluation_status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("evaluation_status IN ('pending', 'approved', 'rejected')", name="ck_whatsapp_ai_prompt_evaluation"),
        sa.UniqueConstraint("client_id", "purpose", "prompt_version", name="uq_whatsapp_ai_prompt_version"),
    )
    op.create_index(
        "uq_whatsapp_ai_one_active",
        "whatsapp_ai_prompt_models",
        ["client_id", "purpose", "schema_version"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "whatsapp_ai_approved_facts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fact_key", sa.String(length=80), nullable=False),
        sa.Column("fact_value", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("client_id", "fact_key", name="uq_whatsapp_ai_fact_key"),
    )
    op.create_table(
        "whatsapp_ai_response_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("response_type", sa.String(length=80), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=False),
        sa.Column("template_body", sa.Text(), nullable=False),
        sa.Column("required_fact_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("client_id", "response_type", "language", name="uq_whatsapp_ai_response_template"),
    )
    op.create_table(
        "whatsapp_conversation_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("client_id", "lead_id", name="uq_whatsapp_conversation_summary"),
    )
    op.create_table(
        "whatsapp_ai_decision_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_key", sa.String(length=36), nullable=False, unique=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("leads.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column("registry_id", sa.Integer(), sa.ForeignKey("whatsapp_ai_prompt_models.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("outbound_intent_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("model_route", sa.String(length=120), nullable=False),
        sa.Column("model_name", sa.String(length=120), nullable=False),
        sa.Column("schema_version", sa.String(length=30), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column("safety_results", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retrieval_references", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("final_outcome", sa.String(length=50), nullable=False),
        sa.Column("escalation_reason", sa.String(length=120), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('REPLY', 'WAIT', 'ESCALATE', 'STOP', 'NO_ACTION')", name="ck_whatsapp_ai_audit_decision"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_whatsapp_ai_audit_confidence"),
    )
    op.create_index("idx_whatsapp_ai_audit_tenant_time", "whatsapp_ai_decision_audits", ["client_id", "created_at"])
    op.add_column("whatsapp_outbound_intents", sa.Column("ai_decision_audit_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_whatsapp_outbound_ai_audit", "whatsapp_outbound_intents", "whatsapp_ai_decision_audits", ["ai_decision_audit_id"], ["id"], ondelete="RESTRICT")
    op.create_index("idx_whatsapp_outbound_ai_audit", "whatsapp_outbound_intents", ["ai_decision_audit_id"], unique=True)

    # Existing prompts are retained only as pending review candidates. Nothing
    # is activated until an explicit offline evaluation has approved route,
    # model, schema, language, and tone together.
    op.execute(
        "INSERT INTO whatsapp_ai_prompt_models "
        "(client_id, purpose, prompt_version, prompt_body, model_route, model_name, schema_version, allowed_languages, tone, evaluation_status, is_active, created_at, updated_at) "
        "SELECT id, 'whatsapp_reply', 'legacy-v2', COALESCE(system_prompt, ''), 'pending', 'pending', 'v2', '[\"en\"]'::jsonb, 'professional', 'pending', false, NOW(), NOW() FROM clients"
    )


def downgrade() -> None:
    raise RuntimeError("Phase 9 AI decision records are forward-only")

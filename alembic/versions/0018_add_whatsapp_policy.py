"""add WhatsApp consent and messaging policy

Revision ID: 0018
Revises: 0017
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("wa_business_account_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "clients",
        sa.Column("wa_access_token_env_var", sa.String(length=100), nullable=True),
    )

    op.create_table(
        "whatsapp_consent_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_reference", sa.String(length=500), nullable=True),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "phone", name="uq_whatsapp_consent_client_phone"),
    )
    op.create_index("idx_whatsapp_consent_lookup", "whatsapp_consent_records", ["client_id", "phone", "revoked_at"])

    op.create_table(
        "whatsapp_opt_outs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=False),
        sa.Column("opted_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("inbound_event_id", sa.String(length=255), nullable=True),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.UniqueConstraint("client_id", "phone", name="uq_whatsapp_opt_out_client_phone"),
    )
    op.create_index("idx_whatsapp_opt_out_lookup", "whatsapp_opt_outs", ["client_id", "phone"])

    op.create_table(
        "whatsapp_tenant_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("outbound_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("quiet_hours_start", sa.String(length=5), nullable=True),
        sa.Column("quiet_hours_end", sa.String(length=5), nullable=True),
        sa.Column("frequency_window_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("max_messages_per_window", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("daily_cap", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("excluded_lead_stages", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='["Booked", "Lost"]'),
        sa.Column("policy_version", sa.String(length=50), nullable=False, server_default="phase7-v1"),
        sa.Column("hot_lead_template_name", sa.String(length=100), nullable=True),
        sa.Column("hot_lead_template_language", sa.String(length=20), nullable=True),
        sa.Column("booking_alert_template_name", sa.String(length=100), nullable=True),
        sa.Column("booking_alert_template_language", sa.String(length=20), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "whatsapp_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("variables", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("approval_status", sa.String(length=30), nullable=False),
        sa.Column("meta_status", sa.String(length=30), nullable=False),
        sa.Column("verification_reference", sa.String(length=500), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta_template_id", sa.String(length=100), nullable=True),
        sa.Column("verified_waba_id", sa.String(length=100), nullable=True),
        sa.Column("verified_phone_number_id", sa.String(length=100), nullable=True),
        sa.Column("meta_variable_count", sa.Integer(), nullable=True),
        sa.Column(
            "component_signature",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_id", "name", "language", "version", name="uq_whatsapp_template_version"),
    )
    op.create_index(
        "idx_whatsapp_template_approved", "whatsapp_templates",
        ["client_id", "name", "language", "approval_status", "retired_at"],
    )

    op.create_table(
        "whatsapp_policy_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("audit_key", sa.String(length=36), nullable=False, unique=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=False),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("session_open", sa.Boolean(), nullable=False),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("whatsapp_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("outbound_intent_id", sa.Integer(), sa.ForeignKey("whatsapp_outbound_intents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("override_reason", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=36), nullable=True),
        sa.Column("provider_outcome", sa.String(length=40), nullable=True),
        sa.Column("provider_failure_category", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_whatsapp_policy_audit_tenant_time", "whatsapp_policy_decisions", ["client_id", "created_at"])
    op.create_index("idx_whatsapp_policy_audit_phone_time", "whatsapp_policy_decisions", ["client_id", "phone", "created_at"])


def downgrade() -> None:
    raise RuntimeError(
        "Phase 7 is forward-only: downgrade is refused so durable WhatsApp "
        "opt-out and policy-audit records cannot be deleted."
    )

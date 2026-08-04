"""add durable WhatsApp operational controls

Revision ID: 0022
Revises: 0021
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_operational_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("control_key", sa.String(length=180), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("control_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column("updated_by", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_whatsapp_operational_control_version"
        ),
        sa.UniqueConstraint(
            "control_key", name="uq_whatsapp_operational_control_key"
        ),
    )
    op.create_index(
        "idx_whatsapp_operational_control_tenant",
        "whatsapp_operational_controls",
        ["client_id", "control_type"],
    )
    op.create_table(
        "whatsapp_operational_control_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "control_id",
            sa.Integer(),
            sa.ForeignKey(
                "whatsapp_operational_controls.id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("control_key", sa.String(length=180), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("control_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("from_enabled", sa.Boolean(), nullable=True),
        sa.Column("to_enabled", sa.Boolean(), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=False),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "control_id",
            "to_version",
            name="uq_whatsapp_operational_control_audit_version",
        ),
        sa.UniqueConstraint(
            "correlation_id",
            name="uq_whatsapp_operational_control_audit_correlation",
        ),
    )
    op.create_index(
        "idx_whatsapp_operational_control_audit_tenant_time",
        "whatsapp_operational_control_audits",
        ["client_id", "created_at"],
    )


    # Global controls must exist before the application can evaluate or mutate
    # them. These enabled version-1 rows preserve existing production behavior
    # while providing a durable row lock for the very first OFF transition.
    bootstrap_controls = (
        (
            "global:global_outbound",
            "global_outbound",
            "00000000-0000-4000-8000-000000000022",
        ),
        (
            "global:worker_consumption",
            "worker_consumption",
            "00000000-0000-4000-8000-000000000023",
        ),
    )
    for control_key, control_type, correlation_id in bootstrap_controls:
        op.execute(
            sa.text(
                """
                INSERT INTO whatsapp_operational_controls (
                    control_key, scope, client_id, control_type, resource_id,
                    enabled, version, updated_by, reason, correlation_id
                ) VALUES (
                    :control_key, 'global', NULL, :control_type, NULL,
                    TRUE, 1, 'system:migration:0022',
                    'phase12a_bootstrap_enabled', :correlation_id
                )
                ON CONFLICT (control_key) DO NOTHING
                """
            ).bindparams(
                control_key=control_key,
                control_type=control_type,
                correlation_id=correlation_id,
            )
        )
        op.execute(
            sa.text(
                """
                INSERT INTO whatsapp_operational_control_audits (
                    control_id, control_key, scope, client_id, control_type,
                    resource_id, from_enabled, to_enabled, from_version,
                    to_version, operator_id, reason, correlation_id
                )
                SELECT
                    id, control_key, scope, client_id, control_type,
                    resource_id, NULL, TRUE, 0, 1,
                    'system:migration:0022',
                    'phase12a_bootstrap_enabled', :correlation_id
                FROM whatsapp_operational_controls
                WHERE control_key = :control_key
                ON CONFLICT (correlation_id) DO NOTHING
                """
            ).bindparams(
                control_key=control_key,
                correlation_id=correlation_id,
            )
        )


def downgrade() -> None:
    raise RuntimeError("Phase 12A operational controls are forward-only")

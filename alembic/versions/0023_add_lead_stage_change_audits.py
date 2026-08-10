"""add canonical lead stage change audits

Revision ID: 0023
Revises: 0022
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lead_stage_change_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            sa.Integer(),
            sa.ForeignKey("leads.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("old_stage", sa.String(length=100), nullable=False),
        sa.Column("new_stage", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_lead_stage_change_audits_tenant_lead_time",
        "lead_stage_change_audits",
        ["client_id", "lead_id", "created_at"],
    )
    op.execute(
        "ALTER TABLE lead_stage_change_audits ENABLE ROW LEVEL SECURITY"
    )


def downgrade() -> None:
    op.drop_index(
        "idx_lead_stage_change_audits_tenant_lead_time",
        table_name="lead_stage_change_audits",
    )
    op.drop_table("lead_stage_change_audits")

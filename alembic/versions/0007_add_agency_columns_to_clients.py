"""add agency role and agency_id columns to clients table

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # role: server_default "owner" backfills every existing client row as a
    # standalone tenant, satisfying NOT NULL without a data migration.
    op.add_column(
        "clients",
        sa.Column("role", sa.String(20), nullable=False, server_default="owner"),
    )
    # agency_id: self-referential FK to clients.id (parent agency). NULL for
    # owners and agencies; set only on sub_account rows.
    op.add_column(
        "clients",
        sa.Column("agency_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_clients_agency_id_clients",
        "clients",
        "clients",
        ["agency_id"],
        ["id"],
    )
    op.create_index("idx_clients_agency_id", "clients", ["agency_id"])


def downgrade() -> None:
    op.drop_index("idx_clients_agency_id", table_name="clients")
    op.drop_constraint("fk_clients_agency_id_clients", "clients", type_="foreignkey")
    op.drop_column("clients", "agency_id")
    op.drop_column("clients", "role")

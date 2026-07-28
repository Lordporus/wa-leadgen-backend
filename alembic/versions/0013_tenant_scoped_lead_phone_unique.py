"""scope lead phone uniqueness to each tenant

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-28
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive preflight for drifted environments. Global phone uniqueness
    # should make this impossible, but never install a weaker constraint over
    # already-invalid tenant data.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM leads
                GROUP BY client_id, phone
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot add uq_leads_client_phone: duplicate tenant phone values exist';
            END IF;
        END
        $$
        """
    )

    # Migration history has used both a unique index and a unique constraint
    # for the global phone invariant. Remove either known form before adding
    # the tenant-scoped constraint.
    op.execute("DROP INDEX IF EXISTS ix_leads_phone")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'leads_phone_key'
                  AND conrelid = 'leads'::regclass
            ) THEN
                ALTER TABLE leads DROP CONSTRAINT leads_phone_key;
            END IF;
        END
        $$
        """
    )
    op.create_unique_constraint(
        "uq_leads_client_phone",
        "leads",
        ["client_id", "phone"],
    )


def downgrade() -> None:
    # Once the same phone exists in multiple tenants, restoring UNIQUE(phone)
    # would be destructive. Refuse the downgrade and require a reviewed
    # forward-fix or explicit data decision.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM leads
                GROUP BY phone
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot restore global lead phone uniqueness: cross-tenant duplicates exist';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "uq_leads_client_phone",
        "leads",
        type_="unique",
    )
    op.create_index(
        "ix_leads_phone",
        "leads",
        ["phone"],
        unique=True,
    )

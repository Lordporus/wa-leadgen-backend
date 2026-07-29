"""enforce one active tenant per WhatsApp phone ID

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-29
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM clients
                WHERE wa_phone_number_id IS NOT NULL
                  AND wa_phone_number_id <> trim(wa_phone_number_id)
            ) THEN
                RAISE EXCEPTION
                    'cannot enforce WhatsApp phone ownership: phone_number_id values must be trimmed';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM clients
                WHERE is_active
                  AND wa_phone_number_id IS NOT NULL
                GROUP BY wa_phone_number_id
                HAVING COUNT(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot enforce WhatsApp phone ownership: active phone_number_id is assigned to multiple tenants';
            END IF;
        END
        $$
        """
    )
    op.create_check_constraint(
        "ck_clients_wa_phone_number_id_trimmed",
        "clients",
        "wa_phone_number_id IS NULL OR wa_phone_number_id = trim(wa_phone_number_id)",
    )
    op.create_index(
        "uidx_clients_active_wa_phone",
        "clients",
        ["wa_phone_number_id"],
        unique=True,
        postgresql_where="wa_phone_number_id IS NOT NULL AND is_active",
    )


def downgrade() -> None:
    op.drop_index("uidx_clients_active_wa_phone", table_name="clients")
    op.drop_constraint(
        "ck_clients_wa_phone_number_id_trimmed",
        "clients",
        type_="check",
    )

"""add hot_lead_threshold to clients table

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

Adds an integer column `hot_lead_threshold` to the `clients` table.
Default value is 70 (matching the existing guardrails.py CONFIDENCE_THRESHOLD).
Range 0-100 enforced at the application layer (PATCH /api/settings).

NOTE: This column is NOT yet wired into the lead-scoring logic. It persists
the setting configured via the Settings UI only. Scoring integration is a
separate task.

Do NOT run this migration until the column has been verified in staging.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column(
            "hot_lead_threshold",
            sa.Integer(),
            nullable=False,
            server_default="70",
        ),
    )


def downgrade() -> None:
    op.drop_column("clients", "hot_lead_threshold")

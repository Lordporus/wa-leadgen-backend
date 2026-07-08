"""add billing columns to clients table

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("razorpay_customer_id", sa.String(100), nullable=True))
    op.add_column("clients", sa.Column("razorpay_subscription_id", sa.String(100), nullable=True))
    op.add_column("clients", sa.Column("subscription_status", sa.String(30), nullable=True, server_default="inactive"))
    op.add_column("clients", sa.Column("plan_tier", sa.String(20), nullable=True, server_default="base"))


def downgrade() -> None:
    op.drop_column("clients", "plan_tier")
    op.drop_column("clients", "subscription_status")
    op.drop_column("clients", "razorpay_subscription_id")
    op.drop_column("clients", "razorpay_customer_id")

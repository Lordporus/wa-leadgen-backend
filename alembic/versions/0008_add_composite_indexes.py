"""add composite indexes for high-frequency query patterns

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-09

Sprint 10 Task 2 — DB indexing review. Adds composite indexes for the
hottest tenant-scoped access patterns identified across main.py / analytics.py
/ usage.py:

  - leads (client_id, status)          — dashboard funnel, stage boards, filters
  - messages (lead_id, direction)      — response-time rollups, IN/OUT counts
  - usage_events (client_id, created_at) — monthly billing-window aggregation

daily_stats (client_id, date) already has `idx_daily_stats_client_date` from
migration 0006 — intentionally NOT recreated here (confirmed present).

NOTE: plain (transactional) CREATE INDEX is used because current per-table row
counts are tiny (early beta). At production scale these should be rebuilt with
CREATE INDEX CONCURRENTLY (outside a transaction) to avoid write locks.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # leads: tenant-scoped status filtering (also serves client_id-only lookups).
    op.create_index("idx_leads_client_status", "leads", ["client_id", "status"])
    # messages: a lead's messages by direction (also serves lead_id-only lookups).
    op.create_index("idx_messages_lead_direction", "messages", ["lead_id", "direction"])
    # usage_events: a client's events within a created_at range (billing window).
    op.create_index(
        "idx_usage_events_client_created", "usage_events", ["client_id", "created_at"]
    )
    # daily_stats (client_id, date): already created in 0006 — no-op here.


def downgrade() -> None:
    op.drop_index("idx_usage_events_client_created", table_name="usage_events")
    op.drop_index("idx_messages_lead_direction", table_name="messages")
    op.drop_index("idx_leads_client_status", table_name="leads")

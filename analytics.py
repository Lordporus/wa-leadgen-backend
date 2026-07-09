"""
Sprint 7 — Analytics & Reporting: nightly rollup jobs.

Aggregates per-tenant daily KPIs from the `leads` and `messages` tables and
persists them into `daily_stats` (one JSONB row per client per day) so the
Sprint 7 Task 2 dashboard endpoints can read pre-computed numbers instead of
scanning the raw tables on every request.

Time handling
-------------
`messages.created_at` / `leads.created_at` are stored as UTC-naive timestamps
(SQLAlchemy default `datetime.utcnow`). "A day" for reporting is an IST
calendar day (00:00–24:00 Asia/Kolkata, UTC+5:30). We therefore convert the
IST day window to a UTC half-open range [start_utc, end_utc) before querying,
so a lead created at 01:00 IST lands in the correct IST day, not the UTC day.

Status buckets are a POINT-IN-TIME snapshot: there is no status-history table,
so new/qualified/booked/lost counts reflect a lead's *current* status filtered
to leads created on that day. total_leads counts leads created that day;
new_leads is the subset still sitting in the "New Lead" stage.
"""

import logging
from datetime import datetime, date as date_type, time, timedelta, timezone

from sqlalchemy import func, and_

from database import SessionLocal, is_configured
from models import Lead, Message, DailyStat
import tenant

logger = logging.getLogger(__name__)

# IST is a fixed offset (no DST) — UTC+5:30.
IST = timezone(timedelta(hours=5, minutes=30))

# Statuses that count as "qualified" for the funnel. Booked leads have passed
# through qualification, so they count as qualified too.
QUALIFIED_STATUSES = {"Qualified", "Booked"}
NEW_STATUS = "New Lead"


def _ist_day_to_utc_range(d: date_type) -> tuple[datetime, datetime]:
    """
    Given an IST calendar date, return the half-open [start, end) window in
    UTC-naive datetimes suitable for comparing against stored created_at values.
    """
    start_ist = datetime.combine(d, time.min, tzinfo=IST)
    end_ist = start_ist + timedelta(days=1)
    # Strip tzinfo after converting to UTC so it compares against naive columns.
    start_utc = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _avg_response_time_seconds(session, client_id: int, start_utc: datetime, end_utc: datetime) -> float | None:
    """
    Average AI response latency for the day: for each OUTBOUND message, the
    gap to the most recent preceding INBOUND message from the same lead.

    Returns None when there were no answerable outbound messages that day
    (keeps the average honest rather than reporting a misleading 0.0).
    """
    outbound = (
        session.query(Message.lead_id, Message.created_at)
        .join(Lead, Lead.id == Message.lead_id)
        .filter(Lead.client_id == client_id)
        .filter(Message.direction == "OUTBOUND")
        .filter(Message.created_at >= start_utc)
        .filter(Message.created_at < end_utc)
        .all()
    )
    if not outbound:
        return None

    total = 0.0
    counted = 0
    for lead_id, out_ts in outbound:
        prev_inbound = (
            session.query(func.max(Message.created_at))
            .filter(Message.lead_id == lead_id)
            .filter(Message.direction == "INBOUND")
            .filter(Message.created_at <= out_ts)
            .scalar()
        )
        if prev_inbound is None:
            continue
        delta = (out_ts - prev_inbound).total_seconds()
        if delta < 0:
            continue
        total += delta
        counted += 1

    if counted == 0:
        return None
    return round(total / counted, 2)


def compute_daily_stats(session, client_id: int, target_date: date_type, won_stages: list[str], lost_stages: list[str]) -> dict:
    """
    Compute (but do not persist) the KPI dict for one client for one IST day.
    Broken out from rollup_daily_stats() so it can be unit-tested without a
    write, and reused by the aggregate rollup path.
    """
    start_utc, end_utc = _ist_day_to_utc_range(target_date)

    # ── Lead buckets (leads CREATED during the IST day) ──────────────────────
    day_leads = (
        session.query(Lead.status, func.count(Lead.id))
        .filter(Lead.client_id == client_id)
        .filter(Lead.created_at >= start_utc)
        .filter(Lead.created_at < end_utc)
        .group_by(Lead.status)
        .all()
    )
    status_counts = {status: count for status, count in day_leads}
    total_leads = sum(status_counts.values())
    new_leads = status_counts.get(NEW_STATUS, 0)
    qualified_leads = sum(c for s, c in status_counts.items() if s in QUALIFIED_STATUSES)
    booked_leads = sum(c for s, c in status_counts.items() if s in won_stages)
    lost_leads = sum(c for s, c in status_counts.items() if s in lost_stages)

    # ── Message buckets (messages SENT during the IST day) ───────────────────
    day_messages = (
        session.query(Message.direction, func.count(Message.id))
        .join(Lead, Lead.id == Message.lead_id)
        .filter(Lead.client_id == client_id)
        .filter(Message.created_at >= start_utc)
        .filter(Message.created_at < end_utc)
        .group_by(Message.direction)
        .all()
    )
    dir_counts = {direction: count for direction, count in day_messages}
    total_messages = sum(dir_counts.values())
    # No human-agent send path exists (takeover only pauses the AI), so every
    # OUTBOUND message is AI-generated; INBOUND messages come from the prospect.
    ai_messages = dir_counts.get("OUTBOUND", 0)
    human_messages = dir_counts.get("INBOUND", 0)

    avg_response_time = _avg_response_time_seconds(session, client_id, start_utc, end_utc)

    # meetings_booked = leads whose CURRENT status is a won stage ("Booked"),
    # created that day. Mirrors the North Star "Meetings Booked" metric.
    meetings_booked = booked_leads

    return {
        "total_leads": total_leads,
        "new_leads": new_leads,
        "qualified_leads": qualified_leads,
        "booked_leads": booked_leads,
        "lost_leads": lost_leads,
        "total_messages": total_messages,
        "ai_messages": ai_messages,
        "human_messages": human_messages,
        "avg_response_time_seconds": avg_response_time,
        "meetings_booked": meetings_booked,
    }


def rollup_daily_stats(client_id: int, date: date_type) -> dict | None:
    """
    Aggregate one client's KPIs for one IST calendar day and UPSERT the result
    into `daily_stats`. Returns the computed stats dict, or None if Postgres is
    not configured. Idempotent: re-running for the same (client_id, date)
    overwrites the existing row.
    """
    if not is_configured():
        logger.warning("Postgres not configured — skipping daily rollup.")
        return None

    won_stages = tenant.get_won_stage_names(client_id)
    lost_stages = tenant.get_lost_stage_names(client_id)

    try:
        with SessionLocal() as session:
            stats = compute_daily_stats(session, client_id, date, won_stages, lost_stages)

            existing = (
                session.query(DailyStat)
                .filter(DailyStat.client_id == client_id)
                .filter(DailyStat.date == date)
                .one_or_none()
            )
            if existing:
                existing.stats = stats
            else:
                session.add(DailyStat(client_id=client_id, date=date, stats=stats))
            session.commit()

        logger.info(f"Daily rollup complete: client {client_id} {date} → {stats}")
        return stats
    except Exception as e:  # noqa: BLE001
        logger.error(f"Daily rollup failed for client {client_id} {date}: {e}")
        return None


def run_nightly_rollup(target_date: date_type | None = None) -> None:
    """
    APScheduler entry point. Runs rollup_daily_stats() for every active tenant
    for YESTERDAY (IST) — the job fires at 02:00 IST, so the day that just
    closed is fully settled. Falls back to single-tenant (client_id=1) when
    Postgres has no client rows (airtable mode).
    """
    if not is_configured():
        logger.info("Nightly rollup skipped — Postgres not configured.")
        return

    if target_date is None:
        # "Yesterday" in IST relative to the 2 AM IST run.
        now_ist = datetime.now(timezone.utc).astimezone(IST)
        target_date = (now_ist - timedelta(days=1)).date()

    logger.info(f"Running nightly rollup for IST date {target_date}...")

    contexts = tenant.get_all_active_clients()
    if not contexts:
        logger.info("No active clients found — running single-tenant rollup (client_id=1).")
        rollup_daily_stats(client_id=1, date=target_date)
        return

    for ctx in contexts:
        rollup_daily_stats(client_id=ctx.client.id, date=target_date)

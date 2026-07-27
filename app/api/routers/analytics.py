from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import text

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.core.database import SessionLocal
from app.core.models import Client

router = APIRouter()

@router.get("/api/analytics/summary", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_summary(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Last-30-days KPI rollup for the authenticated tenant, read from the
    pre-computed `daily_stats` table (populated nightly by analytics.py).

    Returns per-day rows (oldest → newest) plus a summed `totals` block across
    the window. avg_response_time is re-derived as a message-weighted mean of
    the days that had answerable outbound traffic (days with None are skipped),
    so it stays honest rather than averaging in zeros.
    """
    from app.core.models import DailyStat

    # IST "today" — daily_stats keys are IST calendar dates (see analytics.py).
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()
    start_date = today_ist - timedelta(days=30)

    with SessionLocal() as s:
        rows = (
            s.query(DailyStat)
            .filter(DailyStat.client_id == client.id)
            .filter(DailyStat.date >= start_date)
            .order_by(DailyStat.date)
            .all()
        )

        daily = []
        totals = {
            "total_leads": 0, "new_leads": 0, "qualified_leads": 0,
            "booked_leads": 0, "lost_leads": 0, "total_messages": 0,
            "ai_messages": 0, "human_messages": 0, "meetings_booked": 0,
        }
        rt_weighted_sum = 0.0
        rt_weight = 0

        for row in rows:
            st = row.stats or {}
            daily.append({"date": str(row.date), **st})
            for k in totals:
                totals[k] += st.get(k, 0) or 0
            rt = st.get("avg_response_time_seconds")
            ai = st.get("ai_messages", 0) or 0
            if rt is not None and ai > 0:
                rt_weighted_sum += rt * ai
                rt_weight += ai

        totals["avg_response_time_seconds"] = (
            round(rt_weighted_sum / rt_weight, 2) if rt_weight else None
        )
        conv = totals["booked_leads"] / totals["total_leads"] if totals["total_leads"] else 0
        totals["conversion_rate"] = round(conv * 100, 1)

        return {
            "start_date": str(start_date),
            "end_date": str(today_ist),
            "days": len(daily),
            "totals": totals,
            "daily": daily,
        }


@router.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_funnel(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Returns a snapshot count of leads by status for the authenticated client.
    """
    with SessionLocal() as s:
        query = text("""
            SELECT status, COUNT(id) as count
            FROM leads
            WHERE client_id = :client_id
            GROUP BY status
        """)
        results = s.execute(query, {"client_id": client.id}).fetchall()

        return {row.status: row.count for row in results}

@router.get("/api/analytics/response-time", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_response_time(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Average AI response-time trend for the last 7 days, read from the
    pre-computed `daily_stats` table (populated nightly by analytics.py).

    Emits one point per day in the window (oldest → newest). Days with no
    answerable outbound traffic carry avg_seconds = null rather than 0, so the
    frontend can render a gap instead of a misleading dip to zero. The window's
    overall `avg_seconds` is a message-weighted mean across days that had data.
    """
    from app.core.models import DailyStat

    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()
    start_date = today_ist - timedelta(days=7)

    with SessionLocal() as s:
        rows = (
            s.query(DailyStat)
            .filter(DailyStat.client_id == client.id)
            .filter(DailyStat.date >= start_date)
            .order_by(DailyStat.date)
            .all()
        )

        daily = []
        weighted_sum = 0.0
        weight = 0
        for row in rows:
            st = row.stats or {}
            rt = st.get("avg_response_time_seconds")
            ai = st.get("ai_messages", 0) or 0
            daily.append({"date": str(row.date), "avg_seconds": rt})
            if rt is not None and ai > 0:
                weighted_sum += rt * ai
                weight += ai

        return {
            "start_date": str(start_date),
            "end_date": str(today_ist),
            "avg_seconds": round(weighted_sum / weight, 2) if weight else None,
            "daily": daily,
        }

@router.get("/api/analytics/bookings", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_bookings(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Counts bookings by looking at SYSTEM messages indicating a Calendly confirmation.
    Scoped to the last 30 days.
    """
    with SessionLocal() as s:
        query = text("""
            WITH booking_messages AS (
                SELECT m.lead_id, m.created_at
                FROM messages m
                JOIN leads l ON m.lead_id = l.id
                WHERE l.client_id = :client_id
                  AND m.direction = 'SYSTEM'
                  AND m.body ILIKE '%Calendly Booking Confirmed%'
                  AND m.created_at >= CURRENT_DATE - INTERVAL '30 days'
            )
            SELECT
                DATE(created_at) as date,
                COUNT(lead_id) as count
            FROM booking_messages
            GROUP BY DATE(created_at)
            ORDER BY date
        """)

        daily_results = s.execute(query, {"client_id": client.id}).fetchall()

        total = sum(row.count for row in daily_results)

        return {
            "total_bookings": total,
            "daily": [
                {"date": str(row.date), "count": row.count}
                for row in daily_results
            ]
        }

@router.get("/api/analytics/sources", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_sources(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Returns lead counts grouped by source.
    """
    with SessionLocal() as s:
        query = text("""
            SELECT source, COUNT(id) as count
            FROM leads
            WHERE client_id = :client_id
              AND source IS NOT NULL
              AND source != ''
            GROUP BY source
            ORDER BY count DESC
        """)
        results = s.execute(query, {"client_id": client.id}).fetchall()

        return [
            {"source": str(row.source), "count": row.count}
            for row in results
        ]

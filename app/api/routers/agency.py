import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.api.runtime import logger
from app.core.database import SessionLocal
from app.core.models import Client, PipelineStage

router = APIRouter()

class AgencySubAccountBody(BaseModel):
    name: str
    wa_phone_number_id: str | None = None
    system_prompt: str | None = None
    calendly_link: str | None = None
    followup_template: str | None = None
    admin_note: str | None = None


def require_agency(client: Client = Depends(require_api_key)) -> Client:
    """
    Dependency: require_api_key + enforce the caller is an agency tenant.
    Returns the authenticated agency Client row. 403 if role != "agency".
    """
    if client.role != "agency":
        raise HTTPException(status_code=403, detail="Agency role required")
    return client


def _agency_dashboard_url() -> str | None:
    """Public dashboard base URL, from FRONTEND_URL (set in Render for prod)."""
    base = (os.getenv("FRONTEND_URL", "") or "").rstrip("/")
    return base or None


@router.post("/api/agency/sub-accounts")
@limiter.limit("10/minute", key_func=get_client_key)
def create_sub_account(request: Request, response: Response, body: AgencySubAccountBody, client: Client = Depends(require_agency)):
    """
    Provision a new sub-account under the calling agency.

    Creates a child Client row (role="sub_account", agency_id=agency.id),
    generates a dashboard API key (returned once), and seeds default
    pipeline stages so the sub-account dashboard is immediately usable.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    with SessionLocal() as s:
        # ── a. Enforce wa_phone_number_id uniqueness (if supplied) ────
        if body.wa_phone_number_id:
            existing = (
                s.query(Client)
                .filter(Client.wa_phone_number_id == body.wa_phone_number_id)
                .first()
            )
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"wa_phone_number_id '{body.wa_phone_number_id}' is already "
                        f"assigned to client id={existing.id} ({existing.name!r})"
                    ),
                )

        # ── b. Generate raw API key ───────────────────────────────────
        raw_api_key = secrets.token_hex(32)

        # ── c. Compute hash for storage ───────────────────────────────
        key_hash = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()

        # ── d. Insert sub-account client row ──────────────────────────
        new_client = Client(
            name=body.name,
            wa_phone_number_id=body.wa_phone_number_id,
            system_prompt=body.system_prompt,
            calendly_link=body.calendly_link,
            followup_template=body.followup_template or "",
            dashboard_api_key_hash=key_hash,
            is_active=True,
            admin_note=body.admin_note,
            plan_tier="base",
            subscription_status="inactive",
            role="sub_account",
            agency_id=client.id,
        )
        s.add(new_client)
        s.flush()  # get the auto-generated id before seeding stages

        # ── e. Seed default pipeline stages ───────────────────────────
        default_stages = [
            ("New Lead",  1, False, False),
            ("Contacted", 2, False, False),
            ("Qualified", 3, False, False),
            ("Booked",    4, True,  False),
            ("Lost",      5, False, True),
        ]
        for stage_name, position, is_won, is_lost in default_stages:
            s.add(PipelineStage(
                client_id=new_client.id,
                name=stage_name,
                position=position,
                is_won=is_won,
                is_lost=is_lost,
            ))

        s.commit()
        sub_id = new_client.id
        sub_name = new_client.name

    logger.info(
        f"Sprint8: agency id={client.id} provisioned sub-account id={sub_id} name={sub_name!r}"
    )

    # ── f. Return credentials (raw key shown only once) ───────────
    return {
        "id": sub_id,
        "name": sub_name,
        "api_key": raw_api_key,
        "dashboard_url": _agency_dashboard_url(),
    }


@router.get("/api/agency/sub-accounts")
@limiter.limit("60/minute", key_func=get_client_key)
def list_sub_accounts(request: Request, response: Response, client: Client = Depends(require_agency)):
    """List all sub-accounts owned by the calling agency."""
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    with SessionLocal() as s:
        rows = (
            s.query(Client)
            .filter(Client.agency_id == client.id, Client.role == "sub_account")
            .order_by(Client.id)
            .all()
        )
        sub_accounts = [
            {
                "id": c.id,
                "name": c.name,
                "wa_phone_number_id": c.wa_phone_number_id,
                "plan_tier": c.plan_tier,
                "subscription_status": c.subscription_status,
                "is_active": c.is_active,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]

    return {"sub_accounts": sub_accounts, "count": len(sub_accounts)}


@router.get("/api/agency/analytics")
@limiter.limit("120/minute", key_func=get_client_key)
def agency_analytics(request: Request, response: Response, client: Client = Depends(require_agency)):
    """
    Cross-tenant rollup for the calling agency: aggregates the last-30-days
    `daily_stats` (populated nightly by analytics.py) across every sub-account
    where clients.agency_id == agency.id.

    Returns a combined `totals` block summed over all sub-accounts, plus a
    per-sub-account `sub_accounts` breakdown (each with its own totals). Only
    the agency's own children are ever read — no cross-agency data leaks.
    avg_response_time_seconds is a message-weighted mean across days/accounts
    that had answerable outbound traffic (None days skipped, never zero-filled).
    """
    from app.core.models import Client as ClientModel, DailyStat

    # IST "today" — daily_stats keys are IST calendar dates (see analytics.py).
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(timezone.utc).astimezone(IST).date()
    start_date = today_ist - timedelta(days=30)

    _METRIC_KEYS = [
        "total_leads", "new_leads", "qualified_leads", "booked_leads",
        "lost_leads", "total_messages", "ai_messages", "human_messages",
        "meetings_booked",
    ]

    def _empty_totals() -> dict:
        return {k: 0 for k in _METRIC_KEYS}

    with SessionLocal() as s:
        # 1. Resolve this agency's sub-accounts (id + name only).
        subs = (
            s.query(ClientModel)
            .filter(ClientModel.agency_id == client.id, ClientModel.role == "sub_account")
            .order_by(ClientModel.id)
            .all()
        )
        sub_ids = [c.id for c in subs]
        name_by_id = {c.id: c.name for c in subs}

        # Per-sub-account accumulators.
        per_sub = {
            cid: {"totals": _empty_totals(), "rt_weighted_sum": 0.0, "rt_weight": 0}
            for cid in sub_ids
        }
        combined_totals = _empty_totals()
        combined_rt_sum = 0.0
        combined_rt_weight = 0

        if sub_ids:
            rows = (
                s.query(DailyStat)
                .filter(DailyStat.client_id.in_(sub_ids))
                .filter(DailyStat.date >= start_date)
                .all()
            )
            for row in rows:
                bucket = per_sub.get(row.client_id)
                if bucket is None:
                    continue
                st = row.stats or {}
                for k in _METRIC_KEYS:
                    v = st.get(k, 0) or 0
                    bucket["totals"][k] += v
                    combined_totals[k] += v
                rt = st.get("avg_response_time_seconds")
                ai = st.get("ai_messages", 0) or 0
                if rt is not None and ai > 0:
                    bucket["rt_weighted_sum"] += rt * ai
                    bucket["rt_weight"] += ai
                    combined_rt_sum += rt * ai
                    combined_rt_weight += ai

    # 2. Finalize combined totals.
    combined_totals["avg_response_time_seconds"] = (
        round(combined_rt_sum / combined_rt_weight, 2) if combined_rt_weight else None
    )
    combined_conv = (
        combined_totals["booked_leads"] / combined_totals["total_leads"]
        if combined_totals["total_leads"] else 0
    )
    combined_totals["conversion_rate"] = round(combined_conv * 100, 1)

    # 3. Finalize per-sub-account breakdown (preserve sub_ids order).
    sub_breakdown = []
    for cid in sub_ids:
        b = per_sub[cid]
        t = b["totals"]
        t["avg_response_time_seconds"] = (
            round(b["rt_weighted_sum"] / b["rt_weight"], 2) if b["rt_weight"] else None
        )
        conv = t["booked_leads"] / t["total_leads"] if t["total_leads"] else 0
        t["conversion_rate"] = round(conv * 100, 1)
        sub_breakdown.append({"id": cid, "name": name_by_id[cid], "totals": t})

    return {
        "start_date": str(start_date),
        "end_date": str(today_ist),
        "sub_account_count": len(sub_ids),
        "totals": combined_totals,
        "sub_accounts": sub_breakdown,
    }


# ── Document / Knowledge Base endpoints ────────────────────────────────────

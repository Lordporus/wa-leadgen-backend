import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.api.runtime import logger, store, whatsapp
from app.core.database import SessionLocal
from app.core.models import Client, EmailSuppression, Lead, Message
from app.email.email_validation import validate_lead_email

router = APIRouter()

class StageUpdateBody(BaseModel):
    stage: str

def _parse_created_at(raw: str) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip().replace("Z", "").split("+")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return None

def _derive_score_breakdown(score: int) -> dict:
    def cap(v): return max(0, min(100, v))
    return {
        "intent":     cap(score + 7),
        "engagement": cap(score - 8),
        "budget_fit": cap(score - 13),
    }

def _parse_city(last_message: str) -> str:
    """Regex scan for common city mentions in the raw conversation log."""
    cities = [
        "Delhi", "Gurugram", "Noida", "Mumbai", "Bangalore", "Bengaluru",
        "Hyderabad", "Chennai", "Pune", "Kolkata", "Jaipur", "Ahmedabad",
    ]
    for city in cities:
        if re.search(rf"\b{city}\b", last_message, re.IGNORECASE):
            return city
    return "N/A"

def _parse_interest(last_message: str) -> str:
    """Regex scan for dental/medical treatment mentions."""
    treatments = [
        "teeth whitening", "whitening", "braces", "aligners", "implants",
        "root canal", "cleaning", "crown", "veneer", "extraction",
        "consultation", "checkup", "filling",
    ]
    lower = last_message.lower()
    for t in treatments:
        if t in lower:
            return t.title()
    return "N/A"

def _parse_messages(last_message: str) -> list:
    """
    Parse the raw text log format:
      [2026-06-24 10:03:00] INBOUND (text): Hello
      [2026-06-24 10:03:10] OUTBOUND (text): Hi there!
    into the frontend Message array format.
    """
    messages = []
    pattern = re.compile(
        r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(INBOUND|OUTBOUND)\s+\([^)]+\):\s*(.*)",
        re.IGNORECASE,
    )
    for i, line in enumerate(last_message.strip().splitlines()):
        m = pattern.match(line.strip())
        if not m:
            continue
        ts_raw, direction, text = m.group(1), m.group(2).upper(), m.group(3).strip()
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
            time_str = ts.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            time_str = ts_raw
        messages.append({
            "id": f"m{i}",
            "role": "user" if direction == "INBOUND" else "ai",
            "content": text,
            "timestamp": time_str,
        })
    return messages

def _format_lead_row(record: dict) -> dict:
    """Map a raw Airtable record into the leads-list shape."""
    fields = record.get("fields", {})
    raw_score = str(fields.get("Lead_Score", "")).strip().lower()
    if raw_score == "hot":
        score = 90
    elif raw_score == "warm":
        score = 50
    elif raw_score == "cold":
        score = 10
    else:
        score = 0

    raw_created = fields.get("Created_At", "")
    created_dt = _parse_created_at(raw_created)
    created_str = created_dt.strftime("%b %d") if created_dt else "—"

    # last_activity from most recent log line timestamp
    last_msg = fields.get("Last_Message", "")
    last_activity = "—"
    if last_msg:
        lines = [l for l in last_msg.strip().splitlines() if l.strip()]
        if lines:
            m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", lines[-1])
            if m:
                try:
                    msg_dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    diff = datetime.now() - msg_dt
                    if diff.seconds < 120:
                        last_activity = "Just now"
                    elif diff.seconds < 3600:
                        last_activity = f"{diff.seconds // 60} min ago"
                    elif diff.days == 0:
                        last_activity = f"{diff.seconds // 3600} hr ago"
                    else:
                        last_activity = f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
                except ValueError:
                    pass

    # last_message: plain-text preview of the most recent message (≤80 chars)
    last_message_preview = ""
    if last_msg:
        log_lines = [l for l in last_msg.strip().splitlines() if l.strip()]
        if log_lines:
            # Strip the "[YYYY-MM-DD HH:MM:SS] DIRECTION (type): " prefix
            raw_line = log_lines[-1]
            parts = raw_line.split("): ", 1)
            last_message_preview = (parts[1] if len(parts) > 1 else raw_line).strip()[:80]

    return {
        "id":            record["id"],
        "name":          fields.get("Name", "Unknown"),
        "phone":         fields.get("Phone number type", ""),
        "email":         fields.get("email") or None,
        "email_status":  fields.get("email_status") or None,
        "stage":         fields.get("Status", "New Lead"),
        "score":         score,
        "created_at":    created_str,
        "last_activity": last_activity,
        "last_message":  last_message_preview,
    }
# ── Dashboard endpoints ───────────────────────────────────────────────────

@router.get("/api/stats/dashboard")
@limiter.limit("120/minute", key_func=get_client_key)
def get_dashboard_stats(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Aggregate lead counts and 7-day weekly activity from Postgres."""
    with SessionLocal() as s:
        total = s.execute(text("SELECT COUNT(*) FROM leads WHERE client_id = :client_id"), {"client_id": client.id}).scalar() or 0
        booked = s.execute(text("SELECT COUNT(*) FROM leads WHERE client_id = :client_id AND status = 'Booked'"), {"client_id": client.id}).scalar() or 0
        lost = s.execute(text("SELECT COUNT(*) FROM leads WHERE client_id = :client_id AND status = 'Lost'"), {"client_id": client.id}).scalar() or 0

        weekly: dict[str, dict] = {}
        now = datetime.now()
        for i in range(7):
            day = (now - timedelta(days=6 - i)).strftime("%a")
            weekly[day] = {"day": day, "newLeads": 0, "booked": 0}

        recent = s.execute(text("""
            SELECT created_at, status FROM leads
            WHERE client_id = :client_id AND created_at >= CURRENT_DATE - INTERVAL '7 days'
        """), {"client_id": client.id}).fetchall()

        for r in recent:
            if r.created_at:
                day_key = r.created_at.strftime("%a")
                if day_key in weekly:
                    weekly[day_key]["newLeads"] += 1
                    if r.status == "Booked":
                        weekly[day_key]["booked"] += 1

    conversion_rate = round((booked / total * 100)) if total else 0

    return {
        "total":           total,
        "booked":          booked,
        "lost":            lost,
        "conversion_rate": conversion_rate,
        "weekly":          list(weekly.values()),
    }


@router.get("/api/leads")
@limiter.limit("120/minute", key_func=get_client_key)
def list_leads(request: Request, response: Response, client: Client = Depends(require_api_key), stage: str | None = None):
    """Return all leads, optionally filtered by pipeline stage."""
    try:
        if stage:
            records = store._search(f"{{Status}}='{stage}'", client_id=client.id)
        else:
            records = store.get_all_leads(client_id=client.id)
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

    results = []
    if not records:
        return results

    phones = [r.get("fields", {}).get("Phone number type", "") for r in records if r.get("fields", {}).get("Phone number type")]
    phone_to_pg_id = {}
    if phones and SessionLocal:
        with SessionLocal() as s:
            pg_leads = s.query(Lead.phone, Lead.id).filter(Lead.phone.in_(phones), Lead.client_id == client.id).all()
            phone_to_pg_id = {row.phone: row.id for row in pg_leads}

    for r in records:
        row = _format_lead_row(r)
        phone = r.get("fields", {}).get("Phone number type", "")
        pg_id = phone_to_pg_id.get(phone)
        if pg_id:
            row["id"] = pg_id
        results.append(row)

    return results


@router.get("/api/leads/{lead_id}")
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_detail(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Return a single lead with full conversation history."""
    try:
        pg_id = None
        if lead_id.isdigit():
            pg_id = int(lead_id)
            if SessionLocal:
                with SessionLocal() as s:
                    pg_lead = s.query(Lead.phone).filter(Lead.id == pg_id, Lead.client_id == client.id).first()
                    if not pg_lead:
                        raise HTTPException(status_code=404, detail="Lead not found")
                    record = store.get_lead(pg_lead.phone)
            else:
                record = None
        else:
            record = store.get_lead_by_id(lead_id, client_id=client.id)
            if record and SessionLocal:
                phone = record.get("fields", {}).get("Phone number type")
                if phone:
                    with SessionLocal() as s:
                        pg_lead_row = s.query(Lead.id).filter(Lead.phone == phone, Lead.client_id == client.id).first()
                        if pg_lead_row:
                            pg_id = pg_lead_row.id
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid lead ID format")
    except Exception as e:
        logger.error(f"Failed to fetch lead {lead_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")

    fields = record.get("fields", {})
    last_msg = fields.get("Last_Message", "")

    raw_score = str(fields.get("Lead_Score", "")).strip().lower()
    if raw_score == "hot":
        score = 90
    elif raw_score == "warm":
        score = 50
    elif raw_score == "cold":
        score = 10
    else:
        score = 0

    raw_created = fields.get("Created_At", "")
    created_dt = _parse_created_at(raw_created)
    created_str = created_dt.strftime("%b %d, %Y") if created_dt else "—"

    return {
        "id":              pg_id if pg_id else record["id"],
        "name":            fields.get("Name", "Unknown"),
        "phone":           fields.get("Phone number type", ""),
        "city":            _parse_city(last_msg),
        "interest":        _parse_interest(last_msg),
        "stage":           fields.get("Status", "New Lead"),
        "score":           score,
        "score_breakdown": _derive_score_breakdown(score),
        "created_at":      created_str,
        "messages":        _parse_messages(last_msg),
    }

@router.get("/api/leads/{lead_id}/messages")
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_messages(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Return all messages for a lead from Postgres.

    NOTE: This endpoint intentionally bypasses the `store` abstraction and
    queries Postgres directly. Reason: messages are *only* written to Postgres
    (via db_client.append_message), regardless of MIGRATION_MODE. When
    MIGRATION_MODE=dual, `store` routes reads to Airtable (the primary), which
    has no per-message rows — routing through store.get_messages_for_lead()
    would silently return [] and be a functional regression. Direct Postgres
    query is the correct and intentional path here.
    """
    if not lead_id.isdigit():
        raise HTTPException(status_code=404, detail="Lead not found")
    parsed_id = int(lead_id)

    try:

        if not SessionLocal:
            # Postgres not configured; no messages can exist.
            return []

        from app.core.models import Lead, Message
        with SessionLocal() as s:
            # Verify lead exists AND belongs to this tenant (client_id scoping).
            # This is the only authz check needed — we don't go through store here.
            lead = s.query(Lead).filter(
                Lead.id == parsed_id,
                Lead.client_id == client.id,
            ).first()

            if not lead:
                # Return [] rather than 404 — the frontend silently ignores an
                # absent messages list, and SWR would log console errors on 404.
                return []

            msgs = (
                s.query(Message)
                .filter(Message.lead_id == lead.id)
                .order_by(Message.created_at.asc())
                .all()
            )
            result = []
            for m in msgs:
                # Role: inbound = user; human takeover = human; else outbound AI/system
                if m.direction == "INBOUND":
                    role = "user"
                elif (m.msg_type or "").lower() == "human":
                    role = "human"
                else:
                    role = "ai"
                channel = (m.channel or "whatsapp").lower()
                result.append(
                    {
                        "id": f"m{m.id}",
                        "role": role,
                        "content": m.body or "",
                        "timestamp": m.created_at.strftime("%I:%M %p").lstrip("0")
                        if m.created_at
                        else "",
                        "status": m.status,
                        # Multi-channel (Phase E1 / frontend F7)
                        "channel": channel,
                        "subject": m.subject,
                        "msg_type": m.msg_type,
                    }
                )
            return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch messages for lead {lead_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def update_lead_stage(request: Request, response: Response, lead_id: str, body: StageUpdateBody):
    """Update the pipeline stage for a lead by Airtable record ID."""
    valid_stages = {"New Lead", "Contacted", "Qualified", "Booked", "Lost"}
    if body.stage not in valid_stages:
        raise HTTPException(status_code=422, detail=f"Invalid stage. Must be one of: {valid_stages}")
    try:
        result = store.update_lead_status_by_id(lead_id, body.stage)
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

    if not result:
        raise HTTPException(status_code=404, detail="Lead not found or update failed")

    return {"success": True, "stage": body.stage}

@router.post("/api/leads/{lead_id}/takeover", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def takeover_lead(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Human overrides the AI chatbot for this lead."""
    if not lead_id.isdigit():
        raise HTTPException(status_code=404, detail="Lead not found")
    lead_id = int(lead_id)
    from app.core.models import Lead
    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == lead_id, Lead.client_id == client.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.is_human_takeover = True
        s.commit()
    return {"success": True, "lead_id": lead_id, "is_human_takeover": True}

@router.post("/api/leads/{lead_id}/release", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def release_lead(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Human gives control back to the AI chatbot."""
    if not lead_id.isdigit():
        raise HTTPException(status_code=404, detail="Lead not found")
    lead_id = int(lead_id)
    from app.core.models import Lead
    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == lead_id, Lead.client_id == client.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.is_human_takeover = False
        s.commit()
    return {"success": True, "lead_id": lead_id, "is_human_takeover": False}


# ── Lead email management (Phase E4) ──────────────────────────────────────

class LeadEmailUpdateBody(BaseModel):
    """Set or clear a lead's email. Pass email=null or \"\" to clear."""
    email: str | None = None
    email_opt_in_source: str | None = None
    # When true (default), record email_opt_in_at = now if setting a new address.
    mark_opt_in: bool = True


def _lead_email_payload(lead: Lead, *, suppressed: bool = False, suppress_reason: str | None = None) -> dict:
    return {
        "lead_id": lead.id,
        "email": lead.email,
        "email_status": lead.email_status,
        "email_opt_in_at": lead.email_opt_in_at.isoformat() if lead.email_opt_in_at else None,
        "email_opt_in_source": lead.email_opt_in_source,
        "suppressed": suppressed,
        "suppress_reason": suppress_reason,
    }


@router.get("/api/leads/{lead_id}/email")
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_email(
    request: Request,
    response: Response,
    lead_id: str,
    client: Client = Depends(require_api_key),
):
    """Return email fields + suppression status for a lead."""
    if not lead_id.isdigit():
        raise HTTPException(status_code=404, detail="Lead not found")
    lead_id = int(lead_id)

    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == lead_id, Lead.client_id == client.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        suppressed = False
        reason = None
        if lead.email:
            row = (
                s.query(EmailSuppression)
                .filter(
                    EmailSuppression.client_id == client.id,
                    EmailSuppression.email == lead.email.strip().lower(),
                )
                .first()
            )
            if row:
                suppressed = True
                reason = row.reason
        return _lead_email_payload(lead, suppressed=suppressed, suppress_reason=reason)


@router.patch("/api/leads/{lead_id}/email")
@limiter.limit("60/minute", key_func=get_client_key)
def update_lead_email(
    request: Request,
    response: Response,
    lead_id: str,
    body: LeadEmailUpdateBody,
    client: Client = Depends(require_api_key),
):
    """
    Set, update, or clear a lead's email (Phase E4).

    - Validates format + blocks disposable domains
    - Normalizes to lowercase
    - Enforces tenant-scoped uniqueness (409 if another lead owns the address)
    - Does not auto-remove suppressions (compliance: unsub/bounce stay blocked)
    """
    if not lead_id.isdigit():
        raise HTTPException(status_code=404, detail="Lead not found")
    lead_id = int(lead_id)

    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Distinguish "omit email field" vs explicit null — Pydantic gives None for both
    # when default is None; treat any provided body.email through validation.
    # Empty string / null → clear.
    clearing = body.email is None or not str(body.email).strip()

    if not clearing:
        result = validate_lead_email(body.email, allow_empty=False, block_disposable=True)
        if not result.ok:
            raise HTTPException(status_code=400, detail=result.error or "Invalid email")
        new_email = result.email
    else:
        new_email = None

    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == lead_id, Lead.client_id == client.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        if new_email is not None:
            conflict = (
                s.query(Lead)
                .filter(
                    Lead.client_id == client.id,
                    Lead.email == new_email,
                    Lead.id != lead_id,
                )
                .first()
            )
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail=f"Another lead (id={conflict.id}) already uses this email",
                )

            suppression = (
                s.query(EmailSuppression)
                .filter(
                    EmailSuppression.client_id == client.id,
                    EmailSuppression.email == new_email,
                )
                .first()
            )

            previous = (lead.email or "").strip().lower()
            lead.email = new_email

            if suppression:
                # Address is on the do-not-email list — store it but mark status
                # so send path continues to block.
                if suppression.reason == "unsubscribed":
                    lead.email_status = "unsubscribed"
                elif suppression.reason == "complaint":
                    lead.email_status = "complained"
                else:
                    lead.email_status = "bounced"
            elif previous != new_email or not lead.email_status:
                lead.email_status = "unknown"

            if body.mark_opt_in:
                lead.email_opt_in_at = datetime.utcnow()
                if body.email_opt_in_source is not None:
                    lead.email_opt_in_source = (body.email_opt_in_source or "").strip() or None
                elif not lead.email_opt_in_source:
                    lead.email_opt_in_source = "manual"
            elif body.email_opt_in_source is not None:
                lead.email_opt_in_source = (body.email_opt_in_source or "").strip() or None

            suppressed = bool(suppression)
            suppress_reason = suppression.reason if suppression else None
        else:
            lead.email = None
            lead.email_status = None
            lead.email_opt_in_at = None
            lead.email_opt_in_source = None
            suppressed = False
            suppress_reason = None

        lead.updated_at = datetime.utcnow()
        s.commit()
        s.refresh(lead)
        payload = _lead_email_payload(
            lead, suppressed=suppressed, suppress_reason=suppress_reason
        )

    return {"success": True, **payload}


class SendMessageBody(BaseModel):
    message: str

@router.post("/api/leads/{lead_id}/send-message", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def send_human_message(request: Request, response: Response, lead_id: int, body: SendMessageBody, client: Client = Depends(require_api_key)):
    """Send a manual WhatsApp message to the lead."""
    from app.store.db_client import PHONE_KEY

    lead = store.get_lead_by_id(lead_id, client.id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    phone = lead.get("fields", {}).get(PHONE_KEY)
    if not phone:
        logger.error(f"Lead {lead_id} has no phone number on file.")
        raise HTTPException(status_code=400, detail="Lead has no phone number on file")

    try:
        whatsapp.send_message(phone, body.message)
    except Exception as e:
        logger.error("Failed to send manual message", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send WhatsApp message")

    store.append_message(
        phone=phone,
        direction="OUTBOUND",
        message=body.message,
        msg_type="human",
    )
    return {"success": True}



# ── Sprint 8: Agency sub-account endpoints ─────────────────────────────────

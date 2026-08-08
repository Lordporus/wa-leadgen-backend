import re
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.api.runtime import logger, store
from app.core.config import LEGACY_LEAD_ID_COMPAT_ENABLED
from app.core.database import SessionLocal
from app.core.models import Client, EmailSuppression, Lead
from app.email.email_validation import validate_lead_email
from app.services import tenant, whatsapp_inbox, whatsapp_outbox
from app.store.db_client import DatabaseClient

router = APIRouter()

class StageUpdateBody(BaseModel):
    stage: str


def _operation_context(request: Request, response: Response, client_id: int) -> tuple[str, str]:
    raw = request.headers.get("x-request-id", "")
    try:
        correlation_id = str(UUID(raw))
    except (ValueError, TypeError):
        correlation_id = str(uuid4())
    response.headers["X-Correlation-ID"] = correlation_id
    # The current auth model identifies an authenticated tenant session, not
    # an individual user. Never trust a browser-supplied operator identity.
    operator_id = f"tenant:{client_id}:authenticated-session"
    return correlation_id, operator_id


def _inbox_error(
    code: str,
    correlation_id: str,
    *,
    status: int = 409,
    retryable: bool = False,
    intent_id: int | None = None,
    state: str | None = None,
) -> HTTPException:
    detail = {
        "code": code, "correlation_id": correlation_id, "retryable": retryable,
    }
    if intent_id is not None:
        detail["intent_id"] = intent_id
    if state is not None:
        detail["state"] = state
    return HTTPException(status_code=status, detail=detail)


def _is_postgres_store() -> bool:
    return isinstance(store, DatabaseClient)


def _load_postgres_lead(*, lead_id: int | None = None, phone: str | None = None, client_id: int):
    """Resolve a Postgres lead with mandatory tenant scoping."""
    if not SessionLocal or (lead_id is None and not phone):
        return None
    with SessionLocal() as session:
        query = session.query(Lead).filter(Lead.client_id == client_id)
        if lead_id is not None:
            query = query.filter(Lead.id == lead_id)
        else:
            query = query.filter(Lead.phone == phone)
        return query.first()


def _store_record_for_lead_id(lead_id: str, client_id: int) -> dict | None:
    """
    Resolve the public lead ID without changing its dual-mode contract.

    Dual/Airtable mode uses Airtable record IDs. Postgres mode uses numeric
    primary keys serialized as strings. Numeric IDs emitted by the previous
    branch revision remain accepted in dual mode as a scoped legacy fallback.
    """
    normalized = str(lead_id or "").strip()
    if not normalized:
        return None

    if _is_postgres_store():
        if not normalized.isdigit():
            return None
        return store.get_lead_by_id(int(normalized), client_id=client_id)

    if not normalized.isdigit():
        return store.get_lead_by_id(normalized, client_id=client_id)

    if not LEGACY_LEAD_ID_COMPAT_ENABLED:
        return None

    pg_lead = _load_postgres_lead(lead_id=int(normalized), client_id=client_id)
    if not pg_lead:
        return None
    record = store.get_lead(pg_lead.phone, client_id=client_id)
    if record:
        logger.warning(
            "Legacy numeric lead ID resolved in Airtable-backed mode",
            extra={
                "event": "legacy_lead_id_resolved",
                "client_id": client_id,
                "legacy_id_kind": "postgres_numeric",
            },
        )
    return record


def _postgres_lead_id_for_record(
    lead_id: str,
    record: dict,
    client_id: int,
) -> int | None:
    """Return the tenant-scoped Postgres ID behind a public lead record."""
    normalized = str(lead_id or "").strip()
    if _is_postgres_store() and normalized.isdigit():
        return int(normalized)

    phone = record.get("fields", {}).get("Phone number type")
    pg_lead = _load_postgres_lead(
        lead_id=int(normalized) if normalized.isdigit() else None,
        phone=phone,
        client_id=client_id,
    )
    return pg_lead.id if pg_lead else None


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
        lines = [line for line in last_msg.strip().splitlines() if line.strip()]
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
        log_lines = [line for line in last_msg.strip().splitlines() if line.strip()]
        if log_lines:
            raw_line = log_lines[-1]
            parts = raw_line.split("): ", 1)
            last_message_preview = (parts[1] if len(parts) > 1 else raw_line).strip()[:80]

    return {
        "id":            str(record["id"]),
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
    """Aggregate lead counts and 7-day weekly activity from authoritative store data."""
    try:
        all_leads = store.get_all_leads(client_id=client.id)
    except Exception:
        all_leads = []

    won_stages = set(tenant.get_won_stage_names(client.id))
    lost_stages = set(tenant.get_lost_stage_names(client.id))

    total = len(all_leads)
    booked = 0
    lost = 0

    weekly: dict[str, dict] = {}
    now = datetime.now()
    for i in range(7):
        day = (now - timedelta(days=6 - i)).strftime("%a")
        weekly[day] = {"day": day, "newLeads": 0, "booked": 0}

    for record in all_leads:
        fields = record.get("fields", {}) if isinstance(record, dict) else {}
        stage = fields.get("Status", "New Lead")
        if stage in won_stages:
            booked += 1
        elif stage in lost_stages:
            lost += 1

        raw_created = fields.get("Created_At", "")
        created_dt = _parse_created_at(raw_created)
        if created_dt:
            day_key = created_dt.strftime("%a")
            if day_key in weekly:
                weekly[day_key]["newLeads"] += 1
                if stage in won_stages:
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

    return [_format_lead_row(record) for record in records]


@router.get("/api/leads/{lead_id}")
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_detail(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Return a single lead with full conversation history."""
    try:
        record = _store_record_for_lead_id(lead_id, client.id)
    except HTTPException:
        raise
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

    pg_lead_id = _postgres_lead_id_for_record(lead_id, record, client.id)
    pg_lead = _load_postgres_lead(lead_id=pg_lead_id, client_id=client.id) if pg_lead_id else None
    return {
        "id":              str(record["id"]),
        "name":            fields.get("Name", "Unknown"),
        "phone":           fields.get("Phone number type", ""),
        "city":            _parse_city(last_msg),
        "interest":        _parse_interest(last_msg),
        "stage":           fields.get("Status", "New Lead"),
        "score":           score,
        "score_breakdown": _derive_score_breakdown(score),
        "created_at":      created_str,
        "messages":        _parse_messages(last_msg),
        "is_human_takeover": bool(pg_lead.is_human_takeover) if pg_lead else bool(fields.get("is_human_takeover", False)),
        "takeover_version": int(pg_lead.takeover_version or 0) if pg_lead else 0,
        "takeover_owner": pg_lead.takeover_owner if pg_lead else None,
        "takeover_reason": pg_lead.takeover_reason if pg_lead else None,
        "durable_lead_id": pg_lead.id if pg_lead else None,
    }

@router.get("/api/leads/{lead_id}/messages")
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_messages(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Return normalized Postgres messages with an Airtable-history fallback.

    Dual/Postgres mode prefers normalized message rows. Airtable-only mode, or
    a dual-mode lead that has not yet been mirrored, falls back to the scoped
    record's Last_Message history.
    """
    try:
        record = _store_record_for_lead_id(lead_id, client.id)
        if not record:
            return []
        fallback = _parse_messages(record.get("fields", {}).get("Last_Message", ""))
        parsed_id = _postgres_lead_id_for_record(lead_id, record, client.id)
        if parsed_id is None:
            return fallback

        if not SessionLocal:
            return fallback

        result = whatsapp_inbox.timeline(client_id=client.id, lead_id=parsed_id)
        return result or fallback
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch messages for lead {lead_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def update_lead_stage(
    request: Request,
    response: Response,
    lead_id: str,
    body: StageUpdateBody,
    client: Client = Depends(require_api_key),
):
    """Update a lead stage using the active mode's stable public ID."""
    valid_stages = {"New Lead", "Contacted", "Qualified", "Booked", "Lost"}
    if body.stage not in valid_stages:
        raise HTTPException(status_code=422, detail=f"Invalid stage. Must be one of: {valid_stages}")
    try:
        record = _store_record_for_lead_id(lead_id, client.id)
        if not record:
            raise HTTPException(status_code=404, detail="Lead not found or update failed")
        record_id = str(record["id"])
        if _is_postgres_store():
            result = store.update_lead_status_by_id(
                record_id,
                body.stage,
                client_id=client.id,
            )
        else:
            result = store.update_lead_status_by_id(
                record_id,
                body.stage,
                client_id=client.id,
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

    if not result:
        raise HTTPException(status_code=404, detail="Lead not found or update failed")

    return {"success": True, "stage": body.stage}

class TakeoverBody(BaseModel):
    expected_version: int = Field(ge=0)
    reason: str = Field(default="operator_takeover", min_length=1, max_length=255)


class ReleaseBody(BaseModel):
    expected_version: int = Field(ge=0)
    confirmed: bool
    reevaluate_on_next_inbound: bool = False


@router.post("/api/leads/{lead_id}/takeover", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def takeover_lead(request: Request, response: Response, lead_id: str, body: TakeoverBody, client: Client = Depends(require_api_key)):
    """Human overrides the AI chatbot for this lead."""
    record = _store_record_for_lead_id(lead_id, client.id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    pg_lead_id = _postgres_lead_id_for_record(lead_id, record, client.id)
    if pg_lead_id is None:
        raise HTTPException(status_code=503, detail="Durable lead unavailable")
    correlation_id, operator_id = _operation_context(request, response, client.id)
    try:
        state = whatsapp_inbox.transition_takeover(
            client_id=client.id, lead_id=pg_lead_id, enabled=True,
            expected_version=body.expected_version, operator_id=operator_id,
            reason=body.reason, correlation_id=correlation_id, confirmed=True,
        )
    except whatsapp_inbox.InboxConflict as exc:
        raise _inbox_error(str(exc), correlation_id) from exc
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc
    mirror_status = "not_required"
    if not _is_postgres_store():
        try:
            mirror_status = "mirrored" if store.update_human_takeover_by_id(record["id"], True, client_id=client.id) else "failed"
        except Exception:
            mirror_status = "failed"
    return {"success": True, "lead_id": str(record["id"]), "is_human_takeover": True, "takeover_version": state.version, "owner": state.owner, "reason": state.reason, "correlation_id": correlation_id, "mirror_status": mirror_status}

@router.post("/api/leads/{lead_id}/release", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def release_lead(request: Request, response: Response, lead_id: str, body: ReleaseBody, client: Client = Depends(require_api_key)):
    """Human gives control back to the AI chatbot."""
    record = _store_record_for_lead_id(lead_id, client.id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    pg_lead_id = _postgres_lead_id_for_record(lead_id, record, client.id)
    if pg_lead_id is None:
        raise HTTPException(status_code=503, detail="Durable lead unavailable")
    correlation_id, operator_id = _operation_context(request, response, client.id)
    try:
        state = whatsapp_inbox.transition_takeover(
            client_id=client.id, lead_id=pg_lead_id, enabled=False,
            expected_version=body.expected_version, operator_id=operator_id,
            reason="operator_release", correlation_id=correlation_id,
            confirmed=body.confirmed,
        )
    except whatsapp_inbox.InboxConflict as exc:
        raise _inbox_error(str(exc), correlation_id) from exc
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc
    mirror_status = "not_required"
    if not _is_postgres_store():
        try:
            mirror_status = "mirrored" if store.update_human_takeover_by_id(record["id"], False, client_id=client.id) else "failed"
        except Exception:
            mirror_status = "failed"
    if mirror_status == "failed":
        raise _inbox_error("takeover_mirror_failed", correlation_id, status=503, retryable=True)
    return {"success": True, "lead_id": str(record["id"]), "is_human_takeover": False, "takeover_version": state.version, "reevaluation": "next_inbound" if body.reevaluate_on_next_inbound else "not_requested", "correlation_id": correlation_id}


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
    record = _store_record_for_lead_id(lead_id, client.id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    pg_lead_id = _postgres_lead_id_for_record(lead_id, record, client.id)
    if pg_lead_id is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == pg_lead_id, Lead.client_id == client.id).first()
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
    record = _store_record_for_lead_id(lead_id, client.id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    pg_lead_id = _postgres_lead_id_for_record(lead_id, record, client.id)
    if pg_lead_id is None:
        raise HTTPException(status_code=404, detail="Lead not found")

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
        lead = s.query(Lead).filter(Lead.id == pg_lead_id, Lead.client_id == client.id).first()
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
    message: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")

@router.post("/api/leads/{lead_id}/send-message", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def send_human_message(request: Request, response: Response, lead_id: str, body: SendMessageBody, client: Client = Depends(require_api_key)):
    """Send a manual WhatsApp message to the lead."""
    from app.store.db_client import PHONE_KEY

    lead = _store_record_for_lead_id(lead_id, client.id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    phone = lead.get("fields", {}).get(PHONE_KEY)
    if not phone:
        logger.error(f"Lead {lead_id} has no phone number on file.")
        raise HTTPException(status_code=400, detail="Lead has no phone number on file")

    pg_lead_id = _postgres_lead_id_for_record(lead_id, lead, client.id)
    if pg_lead_id is None:
        raise HTTPException(status_code=503, detail="Durable lead unavailable")
    correlation_id, operator_id = _operation_context(request, response, client.id)
    message = body.message.strip()
    if not message:
        raise _inbox_error("empty_manual_message", correlation_id, status=422)
    intent_id: int | None = None
    try:
        intent_id, created = whatsapp_inbox.create_manual_intent(
            client_id=client.id, lead_id=pg_lead_id, body=message,
            idempotency_key=body.idempotency_key, operator_id=operator_id,
            correlation_id=correlation_id,
        )
        result = whatsapp_outbox.process_outbound_intent(intent_id=intent_id, client_id=client.id)
    except whatsapp_inbox.InboxConflict as exc:
        raise _inbox_error(str(exc), correlation_id) from exc
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc
    except Exception as exc:
        logger.error("Durable manual send failed", exc_info=True)
        state = whatsapp_inbox.outbound_intent_state(
            client_id=client.id, intent_id=intent_id
        ) if intent_id is not None else None
        raise _inbox_error(
            "manual_send_failed", correlation_id, status=502,
            retryable=state == "failed", intent_id=intent_id, state=state,
        ) from exc
    if result.state not in {"sent", "unknown"}:
        raise _inbox_error(
            "manual_send_" + result.state, correlation_id, retryable=False,
            intent_id=intent_id, state=result.state,
        )
    return {"success": result.state == "sent", "intent_id": intent_id, "state": result.state, "provider_message_id": result.provider_message_id, "idempotent_replay": not created, "correlation_id": correlation_id}


@router.get("/api/inbox/takeover-queue", dependencies=[Depends(require_api_key)])
def takeover_queue(request: Request, response: Response, client: Client = Depends(require_api_key)):
    correlation_id, _operator_id = _operation_context(request, response, client.id)
    try:
        items = whatsapp_inbox.list_tasks(client_id=client.id)
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc
    return {"items": items, "correlation_id": correlation_id}


@router.get("/api/leads/{lead_id}/operator-actions", dependencies=[Depends(require_api_key)])
def operator_action_history(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    record = _store_record_for_lead_id(lead_id, client.id)
    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")
    pg_lead_id = _postgres_lead_id_for_record(lead_id, record, client.id)
    if pg_lead_id is None:
        raise HTTPException(status_code=503, detail="Durable lead unavailable")
    correlation_id, _operator_id = _operation_context(request, response, client.id)
    try:
        items = whatsapp_inbox.list_operator_actions(
            client_id=client.id, lead_id=pg_lead_id
        )
    except whatsapp_inbox.InboxConflict as exc:
        raise _inbox_error(str(exc), correlation_id, status=404) from exc
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc
    return {"items": items, "correlation_id": correlation_id}


@router.post("/api/inbox/takeover-queue/{task_id}/acknowledge", dependencies=[Depends(require_api_key)])
def acknowledge_takeover_task(request: Request, response: Response, task_id: int, client: Client = Depends(require_api_key)):
    correlation_id, operator_id = _operation_context(request, response, client.id)
    try:
        return whatsapp_inbox.update_task(client_id=client.id, task_id=task_id, operator_id=operator_id, resolve=False, correlation_id=correlation_id)
    except whatsapp_inbox.InboxConflict as exc:
        raise _inbox_error(str(exc), correlation_id, status=404) from exc
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc


@router.post("/api/inbox/takeover-queue/{task_id}/resolve", dependencies=[Depends(require_api_key)])
def resolve_takeover_task(request: Request, response: Response, task_id: int, client: Client = Depends(require_api_key)):
    correlation_id, operator_id = _operation_context(request, response, client.id)
    try:
        return whatsapp_inbox.update_task(client_id=client.id, task_id=task_id, operator_id=operator_id, resolve=True, correlation_id=correlation_id)
    except whatsapp_inbox.InboxConflict as exc:
        raise _inbox_error(str(exc), correlation_id, status=404) from exc
    except whatsapp_inbox.InboxUnavailable as exc:
        raise _inbox_error(str(exc), correlation_id, status=503, retryable=True) from exc



# ── Sprint 8: Agency sub-account endpoints ─────────────────────────────────

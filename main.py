from fastapi import FastAPI, Request, HTTPException, Security, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
import logging
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from apscheduler.schedulers.background import BackgroundScheduler
from config import (
    WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET, LORD_PHONE_NUMBER,
    FOLLOWUP_TEMPLATE_NAME, CLIENT_ID, BLOCKED_NUMBERS,
    ADMIN_SECRET, MIGRATION_MODE,
)
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from calendly_client import CalendlyClient
from store import get_store, get_primary_store, get_secondary_store
from webhook_store import WebhookStore
import tenant
from database import SessionLocal
from sqlalchemy import text
from models import Client, PipelineStage

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

whatsapp = WhatsAppClient()
store = get_store()
calendly = CalendlyClient()

# ── Pydantic request bodies ───────────────────────────────────────────────

class StageUpdateBody(BaseModel):
    stage: str

class SettingsUpdateBody(BaseModel):
    system_prompt: str | None = None
    calendly_link: str | None = None
    wa_phone_number_id: str | None = None

class AdminCreateClientBody(BaseModel):
    name: str
    wa_phone_number_id: str
    system_prompt: str | None = None
    calendly_link: str | None = None
    followup_template: str | None = None
    admin_note: str | None = None

# ── Deduplication: prevent re-processing when Meta retries webhooks ────────
_processed_message_ids: set[str] = set()

def follow_up_job():
    """Hourly job: nudge leads stuck in 'Contacted' for >48h."""
    logger.info("Running hourly follow-up job...")
    clients = tenant.get_all_active_clients()
    if not clients:
        # Fallback for when Postgres isn't configured (airtable mode)
        logger.info("No active clients found (Postgres not configured), running in single-tenant mode.")
        _process_followups_for_client(client_id=1, template_name=FOLLOWUP_TEMPLATE_NAME)
        return

    for ctx in clients:
        template = (ctx.client.followup_template or FOLLOWUP_TEMPLATE_NAME or "").strip()
        _process_followups_for_client(ctx.client.id, template)

def _process_followups_for_client(client_id: int, template_name: str):
    records = store.get_contacted_leads(client_id)
    now = datetime.now()
    for r in records:
        last_msg = r.get("fields", {}).get("Last_Message", "")
        if not last_msg:
            continue
        try:
            lines = last_msg.strip().split('\n')
            time_str = None
            for line in reversed(lines):
                if line.startswith('['):
                    time_str = line.split(']')[0].strip('[')
                    break
            
            if not time_str:
                continue
                
            msg_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            if now - msg_time > timedelta(hours=48):
                phone = r.get("fields", {}).get("Phone number type")
                if template_name:
                    logger.info(f"Follow-up eligible: {phone} (Client {client_id}). Sending template '{template_name}'.")
                    whatsapp.send_template(phone, template_name)
                    store.append_message(phone, direction="outbound",
                                         message=f"[template: {template_name}]", msg_type="template")
                else:
                    logger.info(
                        f"[DRY-RUN] Lead {phone} (Client {client_id}) eligible for follow-up (Contacted > 48h). "
                        f"Set followup_template to send for real."
                    )
        except Exception as e:
            logger.error(f"Error parsing timestamp for follow-up (Client {client_id}): {e}")

def calendly_sync_job():
    logger.info("Running Calendly sync job...")
    bookings = calendly.get_recent_bookings()
    if not bookings:
        logger.info("No recent Calendly bookings found.")
        return
        
    for booking in bookings:
        phone = booking.get("phone")
        if not phone:
            logger.info(f"Unmatched booking (no phone provided): {booking.get('name')}")
            continue
            
        lead = store.get_lead(phone)
        if lead:
            current_status = lead.get("fields", {}).get("Status")
            if current_status not in ("Booked", "Lost"):
                store.update_lead_status(phone, "Booked")
                store.append_message(phone, "system", f"Calendly Booking Confirmed for {booking.get('start_time')}", "system")
                
                admin_phone = LORD_PHONE_NUMBER
                if tenant.is_configured():
                    client_id = lead.get("fields", {}).get("client_id", 1)
                    client_row = tenant.load_client(client_id)
                    if client_row and client_row.admin_phone:
                        admin_phone = client_row.admin_phone
                        
                if admin_phone:
                    whatsapp.send_message(admin_phone, f"📅 BOOKED: {booking.get('name')} booked a call for {booking.get('start_time')}")
                else:
                    logger.warning(f"Booking matched, but neither admin_phone nor LORD_PHONE_NUMBER is configured. Alert suppressed for lead {phone}.")
            else:
                logger.info(f"Matched booking for {phone} but lead status is {current_status}, skipping update.")
        else:
            logger.info(f"Unmatched booking (phone {phone} not in Leads): {booking.get('name')}")

scheduler = BackgroundScheduler()
scheduler.add_job(follow_up_job, 'interval', hours=1)
scheduler.add_job(calendly_sync_job, 'interval', minutes=5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="WhatsApp Acquisition Backend", lifespan=lifespan)

def get_client_key(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    ip = get_remote_address(request)
    return f"client:{api_key}" if api_key else ip

def get_admin_key(request: Request) -> str:
    admin_secret = request.headers.get("X-Admin-Secret")
    ip = get_remote_address(request)
    return f"admin:{admin_secret}:{ip}" if admin_secret else ip

limiter = Limiter(key_func=get_remote_address, headers_enabled=True)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ── Dashboard API key auth ─────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(api_key: str = Security(_api_key_header)) -> Client:
    """
    Dependency: resolve client from API key, with grace-period fallback.
    """
    if not api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

    # 1. Try to resolve via the new hashed per-client key
    ctx = tenant.resolve_context_by_api_key(api_key)
    if ctx:
        return ctx.client

    # 2. Neither matched
    raise HTTPException(status_code=403, detail="Invalid or missing API key")

@app.get("/api/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def get_settings(request: Request, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        return {"system_prompt": "", "calendly_link": "", "wa_phone_number_id": ""}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            return {"system_prompt": "", "calendly_link": "", "wa_phone_number_id": ""}
        return {
            "system_prompt": db_client.system_prompt or "",
            "calendly_link": db_client.calendly_link or "",
            "wa_phone_number_id": db_client.wa_phone_number_id or ""
        }

@app.patch("/api/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def update_settings(request: Request, body: SettingsUpdateBody, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")
        
        if body.system_prompt is not None:
            db_client.system_prompt = body.system_prompt
        if body.calendly_link is not None:
            db_client.calendly_link = body.calendly_link
        if body.wa_phone_number_id is not None:
            db_client.wa_phone_number_id = body.wa_phone_number_id
        
        s.commit()
    
    return {"success": True}

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        os.getenv("FRONTEND_URL", ""),  # set in Render for production
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── F6: Admin onboarding endpoint ─────────────────────────────────────────
_admin_secret_header = APIKeyHeader(name="X-Admin-Secret", auto_error=False)

def require_admin_secret(secret: str = Security(_admin_secret_header)):
    """Dependency: fail closed — rejects if ADMIN_SECRET is unset OR mismatched."""
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET is not configured on the server. Please check environment variables.",
        )
    if not secret or not hmac.compare_digest(secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing admin secret")


@app.post("/api/admin/clients", dependencies=[Depends(require_admin_secret)])
@limiter.limit("10/minute", key_func=get_admin_key)
def admin_create_client(request: Request, body: AdminCreateClientBody):
    """
    Onboard a new client.

    Creates the client row, generates a dashboard API key (returned once),
    and seeds default pipeline stages.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    with SessionLocal() as s:
        # ── a. Check wa_phone_number_id uniqueness ────────────────────
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

        # ── d. Insert client row ──────────────────────────────────────
        new_client = Client(
            name=body.name,
            wa_phone_number_id=body.wa_phone_number_id,
            system_prompt=body.system_prompt,
            calendly_link=body.calendly_link,
            followup_template=body.followup_template or "",
            dashboard_api_key_hash=key_hash,
            is_active=True,
            admin_note=body.admin_note,
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

    logger.info(
        f"F6: onboarded client id={new_client.id} name={body.name!r} "
        f"wa_phone={body.wa_phone_number_id}"
    )

    # ── f. Return credentials (raw key shown only once) ───────────
    return {
        "client_id": new_client.id,
        "name": new_client.name,
        "dashboard_api_key": raw_api_key,
        "wa_phone_number_id": new_client.wa_phone_number_id,
        "pipeline_stages_seeded": len(default_stages),
    }


# ── Dashboard helper utilities ─────────────────────────────────────────────

DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

def _parse_created_at(raw: str) -> datetime | None:
    """Try ISO 8601 and a few common date formats stored in Airtable."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:len(fmt)], fmt)
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
        "stage":         fields.get("Status", "New Lead"),
        "score":         score,
        "created_at":    created_str,
        "last_activity": last_activity,
        "last_message":  last_message_preview,
    }
# ── Dashboard endpoints ───────────────────────────────────────────────────

@app.get("/api/stats/dashboard", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def get_dashboard_stats(request: Request):
    """Aggregate lead counts and 7-day weekly activity from Airtable."""
    try:
        records = store.get_all_leads()
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

    total = booked = lost = 0
    weekly: dict[str, dict] = {}

    now = datetime.now()
    for i in range(7):
        day = (now - timedelta(days=6 - i)).strftime("%a")  # Mon, Tue …
        weekly[day] = {"day": day, "newLeads": 0, "booked": 0}

    for rec in records:
        fields = rec.get("fields", {})
        status = fields.get("Status", "")
        total += 1
        if status == "Booked":  booked += 1
        if status == "Lost":    lost   += 1

        raw_created = fields.get("Created_At", "")
        created_dt = _parse_created_at(raw_created)
        if created_dt and (now - created_dt).days < 7:
            day_key = created_dt.strftime("%a")
            if day_key in weekly:
                weekly[day_key]["newLeads"] += 1
                if status == "Booked":
                    weekly[day_key]["booked"] += 1

    conversion_rate = round((booked / total * 100)) if total else 0

    return {
        "total":           total,
        "booked":          booked,
        "lost":            lost,
        "conversion_rate": conversion_rate,
        "weekly":          list(weekly.values()),
    }


@app.get("/api/leads", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def list_leads(request: Request, stage: str | None = None):
    """Return all leads, optionally filtered by pipeline stage."""
    try:
        if stage:
            records = store._search(f"{{Status}}='{stage}'")
        else:
            records = store.get_all_leads()
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

    return [_format_lead_row(r) for r in records]


@app.get("/api/leads/{lead_id}", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_detail(request: Request, lead_id: str):
    """Return a single lead with full conversation history."""
    try:
        record = store.get_lead_by_id(lead_id)
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

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
        "id":              record["id"],
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

@app.get("/api/leads/{lead_id}/messages", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_messages(request: Request, lead_id: str):
    """Return all messages for a lead from the Postgres Message table."""
    try:
        record = store.get_lead_by_id(lead_id)
        if not record:
            raise HTTPException(status_code=404, detail="Lead not found")
            
        phone = record.get("fields", {}).get("Phone number type")
        if not phone:
            return []
            
        if not SessionLocal:
            return []
            
        from models import Lead, Message
        with SessionLocal() as s:
            lead = s.query(Lead).filter(Lead.phone == phone).first()
            if not lead:
                return []
            
            msgs = s.query(Message).filter(Message.lead_id == lead.id).order_by(Message.created_at).all()
            return [
                {
                    "id": f"m{m.id}",
                    "role": "user" if m.direction == "INBOUND" else "ai",
                    "content": m.body or "",
                    "timestamp": m.created_at.strftime("%I:%M %p").lstrip("0") if m.created_at else "",
                    "status": m.status,
                }
                for m in msgs
            ]
    except Exception as e:
        logger.error(f"Error fetching messages for lead {lead_id}: {e}")
        return []


@app.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def update_lead_stage(request: Request, lead_id: str, body: StageUpdateBody):
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

def verify_signature(payload: bytes, signature_header: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header."""
    if not WHATSAPP_APP_SECRET or not signature_header:
        return False
    expected_sig = hmac.new(
        WHATSAPP_APP_SECRET.encode('utf-8'),
        msg=payload,
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected_sig}", signature_header)


@app.get("/")
@limiter.limit("60/minute")
def read_root(request: Request):
    return {"status": "ok", "message": "WhatsApp Acquisition System is running."}

@app.get("/webhook")
@limiter.limit("10/minute")
def verify_webhook(request: Request):
    """
    Meta Webhook Verification Route.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
            logger.info("Webhook verified successfully.")
            return int(challenge)
        else:
            raise HTTPException(status_code=403, detail="Verification token mismatch")
    
    raise HTTPException(status_code=400, detail="Bad Request")

def _process_analytics_and_extraction_bg(
    sender_phone: str,
    updated_last_message: str,
    user_text: str,
    lead_name: str,
    system_prompt: str | None,
    req_won_stages: list,
    req_lost_stages: list,
    lord_phone: str | None
):
    """
    Background worker that runs analytics (scoring, extraction) and CRM updates
    outside the critical HTTP webhook path.
    """
    from store import get_store
    from gemini_client import GeminiClient
    from whatsapp_client import WhatsAppClient
    
    # Instantiate fresh clients per constraint #1 and #2
    store = get_store()
    req_gemini = GeminiClient(system_prompt=system_prompt)
    whatsapp = WhatsAppClient()
    
    score = None
    
    # 1. Lead Scoring (Independent Try/Except)
    try:
        score = req_gemini.score_lead(updated_last_message)
        store.update_lead_score(sender_phone, score)
    except Exception as e:
        logger.error(f"Lead scoring failed in background: {e}")
        
    # 2. Information Extraction (Independent Try/Except)
    try:
        info = req_gemini.extract_lead_info(updated_last_message)
        if info:
            store.update_lead_info(
                sender_phone,
                name=info.get("Name"),
                business_name=info.get("Business_Name"),
            )
    except Exception as e:
        logger.error(f"Lead info extraction failed in background: {e}")
        
    # 3. Status Updates (Independent Try/Except)
    try:
        if score in req_won_stages:
            store.update_lead_status(sender_phone, "Qualified")
        elif score == "Cold":
            decline_keywords = ["not interested", "stop", "no", "nahi", "cancel", "unsubscribe"]
            if any(word in user_text.lower() for word in decline_keywords):
                lost_stage = req_lost_stages[0] if req_lost_stages else "Lost"
                store.update_lead_status(sender_phone, lost_stage)
                logger.info(f"Lead {sender_phone} marked as {lost_stage} due to explicit decline.")
    except Exception as e:
        logger.error(f"Status update failed in background: {e}")
        
    # 4. Lord Notification (Executed last, constraint #4)
    try:
        if score in req_won_stages:
            if lord_phone:
                norm_lord = lord_phone.replace('+', '').replace(' ', '').replace('-', '')
                if store.get_lead(norm_lord):
                    logger.error(
                        f"ALERT SUPPRESSED: LORD_PHONE_NUMBER ({lord_phone}) matches an "
                        f"existing lead record. Update LORD_PHONE_NUMBER in .env to avoid loop."
                    )
                else:
                    whatsapp.send_message(lord_phone, f"🔥 HOT LEAD ALERT: Check Airtable for {lead_name} ({sender_phone})")
            else:
                logger.info(f"🔥 HOT LEAD: {lead_name} {sender_phone}")
    except Exception as e:
        logger.error(f"Lord notification failed in background: {e}")

@app.post("/webhook")
@limiter.limit("1000/minute")
async def receive_message(request: Request, bg_tasks: BackgroundTasks):
    """
    Receive incoming messages from WhatsApp users.
    """
    # 1. Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    body_bytes = await request.body()
    if WHATSAPP_APP_SECRET and not verify_signature(body_bytes, signature):
        logger.warning("Invalid webhook signature rejected.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()
    
    # Use decoupled background-capable store for webhook flow
    store = WebhookStore(get_primary_store(), get_secondary_store(), bg_tasks)
    
    if body.get("object") == "whatsapp_business_account":
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                
                # ── F6: Webhook routing by phone_number_id ────────────────
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                ctx = tenant.resolve_context_by_phone_id(phone_number_id) if phone_number_id else None
                
                if not ctx:
                    if MIGRATION_MODE == "airtable" or not tenant.is_configured():
                        # Fallback for client #1 during transition / airtable mode
                        fallback_client = tenant.load_client(CLIENT_ID)
                        req_gemini = tenant.get_gemini_for_client(fallback_client)
                        req_won_stages = tenant.get_won_stage_names(CLIENT_ID)
                        req_lost_stages = tenant.get_lost_stage_names(CLIENT_ID)
                    else:
                        logger.warning(f"Unknown phone_number_id: {phone_number_id}")
                        return {"status": "ignored"}
                else:
                    req_gemini = ctx.gemini
                    req_won_stages = ctx.won_stages
                    req_lost_stages = ctx.lost_stages

                # Check for incoming messages
                if "messages" in value:
                    for message in value["messages"]:
                        sender_phone = message.get("from")
                        message_type = message.get("type")
                        
                        # ── FIX 1: Hard guard — ignore any message from LORD_PHONE_NUMBER ──
                        # Normalise both sides (strip +, spaces, dashes) before comparing.
                        normalized_sender = sender_phone.replace('+', '').replace(' ', '').replace('-', '') if sender_phone else ''
                        normalized_lord   = LORD_PHONE_NUMBER.replace('+', '').replace(' ', '').replace('-', '') if LORD_PHONE_NUMBER else ''
                        if normalized_lord and normalized_sender == normalized_lord:
                            logger.warning(f"Ignored: message from LORD_PHONE_NUMBER ({sender_phone}) — loop guard triggered.")
                            continue

                        # ── Dedup guard: skip messages already processed ──
                        msg_id = message.get("id", "")
                        if msg_id in _processed_message_ids:
                            logger.info(f"Duplicate message {msg_id} from {sender_phone}, skipping")
                            continue
                        if len(_processed_message_ids) > 1000:
                            _processed_message_ids.clear()
                        _processed_message_ids.add(msg_id)

                        if message_type == "text":
                            user_text = message["text"]["body"]
                            logger.info(f"Received message from {sender_phone}: {user_text}")
                            
                            lead = store.get_lead(sender_phone)
                            if not lead:
                                # ── Guard 1: Meta test numbers (start with 1555) ──
                                if sender_phone and sender_phone.lstrip('+').startswith('1555'):
                                    logger.info(f"Ignored Meta test number: {sender_phone}")
                                    continue

                                # ── Guard 2: Manually blocked numbers ────────────
                                normalized_sender_clean = sender_phone.replace('+', '').replace(' ', '').replace('-', '') if sender_phone else ''
                                blocked_clean = [n.replace('+', '').replace(' ', '').replace('-', '') for n in BLOCKED_NUMBERS]
                                if normalized_sender_clean in blocked_clean:
                                    logger.info(f"Ignored blocked number: {sender_phone}")
                                    continue

                                # ── Auto-create new inbound lead ─────────────────
                                logger.info(f"New unknown number {sender_phone} — creating lead automatically.")
                                new_record = store.add_lead(
                                    name="Unknown",
                                    phone=sender_phone,
                                    source="Inbound WhatsApp",
                                )
                                if not new_record:
                                    logger.error(f"Failed to create lead for {sender_phone}. Dropping message.")
                                    continue
                                
                                lead = new_record
                                
                            # If matched: log message
                            store.append_message(sender_phone, direction="inbound", message=user_text, msg_type="text")
                            
                            current_status = lead.get("fields", {}).get("Status", "New Lead")
                            
                            # Update lead status to "Contacted" if currently "New Lead"
                            if current_status == "New Lead":
                                store.update_lead_status(sender_phone, "Contacted")
                                
                            # Phase 4: AI Routing & Scoring
                            last_message = lead.get("fields", {}).get("Last_Message", "")
                            # Manually append the inbound message to memory for Gemini to parse
                            updated_last_message = last_message + f"\n[INBOUND - text]\n{user_text}\n"
                            
                            parsed_history = req_gemini.parse_conversation_history(updated_last_message)
                            
                            ai_reply = req_gemini.generate_response_with_history(parsed_history, user_text)
                            wamid = whatsapp.send_message(sender_phone, ai_reply)
                            store.append_message(sender_phone, direction="outbound", message=ai_reply, msg_type="text", wa_message_id=wamid)
                            
                            # Manually append the outbound message to memory for scoring
                            updated_last_message += f"\n[OUTBOUND - text]\n{ai_reply}\n"
                            
                            # Submit remaining LLM processing + CRM + Alerts to Background Tasks
                            lead_name = lead.get("fields", {}).get("Name", "Unknown") if isinstance(lead, dict) else lead.business_name
                            
                            bg_tasks.add_task(
                                _process_analytics_and_extraction_bg,
                                sender_phone,
                                updated_last_message,
                                user_text,
                                lead_name,
                                getattr(req_gemini, "system_prompt", None),
                                req_won_stages,
                                req_lost_stages,
                                LORD_PHONE_NUMBER
                            )

                            
                # Check for message status updates (delivered/read)
                elif "statuses" in value:
                    for status in value["statuses"]:
                        wamid = status["id"]
                        status_str = status["status"]
                        logger.info(f"Message {wamid} to {status['recipient_id']} status: {status_str}")
                        store.update_message_status(wamid, status_str)
                            
        return {"status": "success"}
    return {"status": "ignored"}


@app.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_funnel(request: Request, client: Client = Depends(require_api_key)):
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

@app.get("/api/analytics/response-time", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_response_time(request: Request, client: Client = Depends(require_api_key)):
    """
    Pairs each INBOUND message with the next OUTBOUND message to calculate response times.
    Uses Postgres window functions to determine the exact gap.
    """
    with SessionLocal() as s:
        # Overall aggregates (Average, Median, Max)
        stats_query = text("""
            WITH paired_messages AS (
                SELECT 
                    m.lead_id,
                    m.direction,
                    m.created_at as inbound_time,
                    LEAD(m.created_at) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_time,
                    LEAD(m.direction) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_direction
                FROM messages m
                JOIN leads l ON m.lead_id = l.id
                WHERE l.client_id = :client_id
            ),
            response_times AS (
                SELECT 
                    EXTRACT(EPOCH FROM (next_time - inbound_time)) as response_time_seconds
                FROM paired_messages
                WHERE direction = 'INBOUND' AND next_direction = 'OUTBOUND'
            )
            SELECT 
                COALESCE(AVG(response_time_seconds), 0) as avg_seconds,
                COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY response_time_seconds), 0) as median_seconds,
                COALESCE(MAX(response_time_seconds), 0) as max_seconds
            FROM response_times
        """)
        
        stats = s.execute(stats_query, {"client_id": client.id}).fetchone()
        
        # Time-series (last 14 days)
        daily_query = text("""
            WITH paired_messages AS (
                SELECT 
                    m.lead_id,
                    m.direction,
                    m.created_at as inbound_time,
                    LEAD(m.created_at) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_time,
                    LEAD(m.direction) OVER (PARTITION BY m.lead_id ORDER BY m.created_at) as next_direction
                FROM messages m
                JOIN leads l ON m.lead_id = l.id
                WHERE l.client_id = :client_id
                  AND m.created_at >= CURRENT_DATE - INTERVAL '14 days'
            ),
            response_times AS (
                SELECT 
                    DATE(inbound_time) as date,
                    EXTRACT(EPOCH FROM (next_time - inbound_time)) as response_time_seconds
                FROM paired_messages
                WHERE direction = 'INBOUND' AND next_direction = 'OUTBOUND'
            )
            SELECT 
                date,
                AVG(response_time_seconds) as avg_seconds
            FROM response_times
            GROUP BY date
            ORDER BY date
        """)
        
        daily_results = s.execute(daily_query, {"client_id": client.id}).fetchall()
        
        return {
            "avg_seconds": round(float(stats.avg_seconds), 2) if stats and stats.avg_seconds else 0,
            "median_seconds": round(float(stats.median_seconds), 2) if stats and stats.median_seconds else 0,
            "max_seconds": round(float(stats.max_seconds), 2) if stats and stats.max_seconds else 0,
            "daily": [
                {"date": str(row.date), "avg_seconds": round(float(row.avg_seconds), 2)}
                for row in daily_results
            ]
        }

@app.get("/api/analytics/bookings", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def analytics_bookings(request: Request, client: Client = Depends(require_api_key)):
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

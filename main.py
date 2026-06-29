from fastapi import FastAPI, Request, HTTPException, Security, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
import logging
import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from config import (
    WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET, LORD_PHONE_NUMBER,
    FOLLOWUP_TEMPLATE_NAME, CLIENT_ID, DASHBOARD_API_KEY, BLOCKED_NUMBERS,
)
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from calendly_client import CalendlyClient
from store import get_store
import tenant

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

whatsapp = WhatsAppClient()
store = get_store()
calendly = CalendlyClient()

# ── Phase 8: load per-client config once at startup ───────────────────────
# In airtable mode, load_client() returns None and get_gemini_for_client()
# falls back to the hardcoded DEFAULT_SYSTEM_PROMPT — zero behaviour change.
_active_client = tenant.load_client(CLIENT_ID)
gemini = tenant.get_gemini_for_client(_active_client)
_won_stages  = tenant.get_won_stage_names(CLIENT_ID)   # e.g. ['Booked']
_lost_stages = tenant.get_lost_stage_names(CLIENT_ID)  # e.g. ['Lost']

logger.info(f"Phase 8 tenant config: client_id={CLIENT_ID} won={_won_stages} lost={_lost_stages}")

# ── Deduplication: prevent re-processing when Meta retries webhooks ────────
_processed_message_ids: set[str] = set()

def follow_up_job():
    """Hourly job: nudge leads stuck in 'Contacted' for >48h."""
    logger.info("Running hourly follow-up job...")
    records = store._search("{Status}='Contacted'")
    now = datetime.now()
    for r in records:
        last_msg = r.get("fields", {}).get("Last_Message", "")
        if not last_msg:
            continue
        try:
            last_line = last_msg.strip().split('\n')[-1]
            time_str = last_line.split(']')[0].strip('[')
            msg_time = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            if now - msg_time > timedelta(hours=48):
                phone = r.get("fields", {}).get("Phone number type")
                if FOLLOWUP_TEMPLATE_NAME:
                    logger.info(f"Follow-up eligible: {phone}. Sending template '{FOLLOWUP_TEMPLATE_NAME}'.")
                    whatsapp.send_template(phone, FOLLOWUP_TEMPLATE_NAME)
                    store.append_message(phone, direction="outbound",
                                         message=f"[template: {FOLLOWUP_TEMPLATE_NAME}]", msg_type="template")
                else:
                    logger.info(
                        f"[DRY-RUN] Lead {phone} eligible for follow-up (Contacted > 48h). "
                        f"Set FOLLOWUP_TEMPLATE_NAME to send for real."
                    )
        except Exception as e:
            logger.error(f"Error parsing timestamp for follow-up: {e}")

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
            if current_status == "Qualified":
                store.update_lead_status(phone, "Booked")
                store.append_message(phone, "system", f"Calendly Booking Confirmed for {booking.get('start_time')}", "system")
                if LORD_PHONE_NUMBER:
                    whatsapp.send_message(LORD_PHONE_NUMBER, f"📅 BOOKED: {booking.get('name')} booked a call for {booking.get('start_time')}")
            else:
                logger.info(f"Matched booking for {phone} but lead status is {current_status}, not Qualified.")
        else:
            logger.info(f"Unmatched booking (phone {phone} not in Leads): {booking.get('name')}")

scheduler = BackgroundScheduler()
scheduler.add_job(follow_up_job, 'interval', hours=1)
scheduler.add_job(calendly_sync_job, 'interval', hours=1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="WhatsApp Acquisition Backend", lifespan=lifespan)

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

# ── Dashboard API key auth ─────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_api_key(api_key: str = Security(_api_key_header)):
    """Dependency: reject requests that don't carry the correct X-API-Key."""
    if not DASHBOARD_API_KEY:
        # Key not configured → open access (dev convenience; warn loudly).
        logger.warning("DASHBOARD_API_KEY is not set — dashboard endpoints are unprotected!")
        return
    if api_key != DASHBOARD_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

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
    raw_score = fields.get("Lead_Score") or 0
    try:
        score = int(float(raw_score))
    except (ValueError, TypeError):
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


# ── Pydantic request bodies ───────────────────────────────────────────────

class StageUpdateBody(BaseModel):
    stage: str


# ── Dashboard endpoints ───────────────────────────────────────────────────

@app.get("/api/stats/dashboard", dependencies=[Depends(require_api_key)])
def get_dashboard_stats():
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
def list_leads(stage: str | None = None):
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
def get_lead_detail(lead_id: str):
    """Return a single lead with full conversation history."""
    try:
        record = store.get_lead_by_id(lead_id)
    except Exception:
        raise HTTPException(status_code=503, detail="data source unavailable")

    if not record:
        raise HTTPException(status_code=404, detail="Lead not found")

    fields = record.get("fields", {})
    last_msg = fields.get("Last_Message", "")

    raw_score = fields.get("Lead_Score") or 0
    try:
        score = int(float(raw_score))
    except (ValueError, TypeError):
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


@app.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])
def update_lead_stage(lead_id: str, body: StageUpdateBody):
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
def read_root():
    return {"status": "ok", "message": "WhatsApp Acquisition System is running."}

@app.get("/webhook")
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

@app.post("/webhook")
async def receive_message(request: Request):
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
    
    if body.get("object") == "whatsapp_business_account":
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                
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

                                # Re-fetch so `lead` has the full Airtable record shape
                                # (same structure that get_lead() returns)
                                lead = store.get_lead(sender_phone)
                                if not lead:
                                    logger.error(f"Lead created but not retrievable for {sender_phone}. Dropping message.")
                                    continue
                                
                            # If matched: log message
                            store.append_message(sender_phone, direction="inbound", message=user_text, msg_type="text")
                            
                            # Refresh lead to get updated Last_Message
                            lead = store.get_lead(sender_phone)
                            current_status = lead.get("fields", {}).get("Status")
                            
                            # Update lead status to "Contacted" if currently "New Lead"
                            if current_status == "New Lead":
                                store.update_lead_status(sender_phone, "Contacted")
                                
                            # Phase 4: AI Routing & Scoring
                            last_message = lead.get("fields", {}).get("Last_Message", "")
                            parsed_history = gemini.parse_conversation_history(last_message)
                            
                            ai_reply = gemini.generate_response_with_history(parsed_history, user_text)
                            whatsapp.send_message(sender_phone, ai_reply)
                            store.append_message(sender_phone, direction="outbound", message=ai_reply, msg_type="text")
                            
                            # Refresh lead to score the full conversation including the outbound message
                            lead_after_reply = store.get_lead(sender_phone)
                            updated_last_message = lead_after_reply.get("fields", {}).get("Last_Message", "")
                            
                            score = gemini.score_lead(updated_last_message)
                            store.update_lead_score(sender_phone, score)

                            # Phase 7: extract name/business from the live conversation (was previously dead code)
                            try:
                                info = gemini.extract_lead_info(updated_last_message)
                                if info:
                                    store.update_lead_info(
                                        sender_phone,
                                        name=info.get("Name"),
                                        business_name=info.get("Business_Name"),
                                    )
                            except Exception as e:
                                logger.error(f"Lead info extraction failed: {e}")
                            
                            if score in _won_stages:
                                store.update_lead_status(sender_phone, "Qualified")
                                if LORD_PHONE_NUMBER:
                                    # ── FIX 2: Defense-in-depth — never alert to a number that is also an Airtable lead ──
                                    norm_lord = LORD_PHONE_NUMBER.replace('+', '').replace(' ', '').replace('-', '')
                                    if store.get_lead(norm_lord):
                                        logger.error(
                                            f"ALERT SUPPRESSED: LORD_PHONE_NUMBER ({LORD_PHONE_NUMBER}) matches an "
                                            f"existing lead record. Update LORD_PHONE_NUMBER in .env to avoid loop."
                                        )
                                    else:
                                        whatsapp.send_message(LORD_PHONE_NUMBER, f"🔥 HOT LEAD ALERT: Check Airtable for {lead.get('fields', {}).get('Name', 'Unknown')} ({sender_phone})")
                                else:
                                    logger.info(f"🔥 HOT LEAD: {lead.get('fields', {}).get('Name', 'Unknown')} {sender_phone}")
                            elif score == "Cold":
                                decline_keywords = ["not interested", "stop", "no", "nahi", "cancel", "unsubscribe"]
                                if any(word in user_text.lower() for word in decline_keywords):
                                    lost_stage = _lost_stages[0] if _lost_stages else "Lost"
                                    store.update_lead_status(sender_phone, lost_stage)
                                    logger.info(f"Lead {sender_phone} marked as {lost_stage} due to explicit decline.")

                            
                # Check for message status updates (delivered/read)
                elif "statuses" in value:
                    for status in value["statuses"]:
                        logger.info(f"Message {status['id']} to {status['recipient_id']} status: {status['status']}")
                            
        return {"status": "success"}
    return {"status": "ignored"}

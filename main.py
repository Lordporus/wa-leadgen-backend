from fastapi import FastAPI, Request, HTTPException, Security, Depends, BackgroundTasks, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
import logging
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware
from slowapi.errors import RateLimitExceeded
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from config import (
    WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET, LORD_PHONE_NUMBER,
    FOLLOWUP_TEMPLATE_NAME, CLIENT_ID, BLOCKED_NUMBERS,
    ADMIN_SECRET, MIGRATION_MODE, JWT_SECRET, REDIS_URL,
    SENTRY_DSN, SENTRY_ENVIRONMENT, SENTRY_TRACES_SAMPLE_RATE,
)
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from calendly_client import CalendlyClient
from store import get_store, get_primary_store, get_secondary_store
from webhook_store import WebhookStore
import tenant
from database import SessionLocal
from sqlalchemy import text
from models import Client, PipelineStage, PromptTemplate
from redis import Redis
from rq import Queue as RQQueue
import analytics

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Sentry APM ────────────────────────────────────────────────────────────
# Initialize before the FastAPI app is created so the ASGI integration
# instruments every request. No-op when SENTRY_DSN is unset (local dev).
if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
    )
    logger.info(f"Sentry APM initialized (env={SENTRY_ENVIRONMENT})")
else:
    logger.info("Sentry APM disabled (SENTRY_DSN not set)")

whatsapp = WhatsAppClient()
store = get_store()
calendly = CalendlyClient()

# ── Redis queue for webhook jobs ─────────────────────────────────────────
redis_conn = Redis.from_url(
    REDIS_URL,
    socket_timeout=2,
    socket_connect_timeout=2,
    retry_on_timeout=True,
    health_check_interval=30,
) if REDIS_URL else None
webhook_queue = RQQueue("webhooks", connection=redis_conn) if redis_conn else None

# ── Pydantic request bodies ───────────────────────────────────────────────

class StageUpdateBody(BaseModel):
    stage: str

class PipelineStageUpdate(BaseModel):
    id: int
    name: str
    is_won: bool
    is_lost: bool

class SettingsUpdateBody(BaseModel):
    system_prompt: str | None = None
    calendly_link: str | None = None
    wa_phone_number_id: str | None = None
    pipeline_stages: list[PipelineStageUpdate] | None = None
    brand_color: str | None = None
    logo_url: str | None = None
    company_display_name: str | None = None
    hot_lead_threshold: int | None = None

class AdminCreateClientBody(BaseModel):
    name: str
    wa_phone_number_id: str
    system_prompt: str | None = None
    calendly_link: str | None = None
    followup_template: str | None = None
    admin_note: str | None = None


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
# Nightly analytics rollup — 02:00 IST every day. Rolls up YESTERDAY (IST) for
# every active tenant. CronTrigger timezone is explicit so it fires at 2 AM IST
# regardless of the host/container timezone (Render runs UTC).
scheduler.add_job(
    analytics.run_nightly_rollup,
    CronTrigger(hour=2, minute=0, timezone="Asia/Kolkata"),
    id="nightly_rollup",
    replace_existing=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(title="WhatsApp Acquisition Backend", lifespan=lifespan)

if not WHATSAPP_APP_SECRET:
    raise RuntimeError(
        "WHATSAPP_APP_SECRET must be set. "
        "Refusing to start without webhook signature verification."
    )

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

# ── JWT Authentication ────────────────────────────────────────────────────
import jwt as pyjwt

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_HOURS = 24

class LoginBody(BaseModel):
    api_key: str

def verify_jwt(request: Request) -> Client:
    """
    Dependency: decode and verify a JWT from the Authorization header.
    Returns the Client ORM row for the authenticated tenant.
    Fails closed: missing/invalid/expired token → 401.
    """
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured on the server")

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = auth_header.split(" ", 1)[1]
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    client_id = payload.get("client_id")
    if not client_id:
        raise HTTPException(status_code=401, detail="Token missing client_id")

    client = tenant.load_client(client_id)
    if not client or not client.is_active:
        raise HTTPException(status_code=401, detail="Client not found or inactive")

    return client


def require_admin(request: Request) -> Client:
    """
    Dependency: verify JWT and enforce role == "admin".
    Returns the Client ORM row. Raises 403 if the token is valid but
    the role is not admin.
    """
    client = verify_jwt(request)

    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured on the server")

    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1]
    payload = pyjwt.decode(token, JWT_SECRET, algorithms=[_JWT_ALGORITHM])

    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    return client


@app.post("/auth/login")
@limiter.limit("10/minute")
def login(request: Request, response: Response, body: LoginBody):
    """
    Authenticate with a client API key and receive a signed JWT.
    The raw API key is validated against the hashed key in the clients table.
    """
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured on the server")

    ctx = tenant.resolve_context_by_api_key(body.api_key)
    if not ctx:
        raise HTTPException(status_code=401, detail="Invalid API key")

    now = datetime.utcnow()
    payload = {
        "client_id": ctx.client.id,
        "tenant_id": ctx.client.id,
        "role": "admin",
        "iat": now,
        "exp": now + timedelta(hours=_JWT_EXPIRY_HOURS),
    }
    token = pyjwt.encode(payload, JWT_SECRET, algorithm=_JWT_ALGORITHM)

    return {"access_token": token, "token_type": "bearer", "expires_in": _JWT_EXPIRY_HOURS * 3600}

@app.get("/api/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def get_settings(request: Request, response: Response, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        return {"system_prompt": "", "calendly_link": "", "wa_phone_number_id": "", "pipeline_stages": [], "brand_color": "#C8A96E", "logo_url": "", "company_display_name": "Leadgen CRM", "hot_lead_threshold": 70}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            return {"system_prompt": "", "calendly_link": "", "wa_phone_number_id": "", "pipeline_stages": [], "brand_color": "#C8A96E", "logo_url": "", "company_display_name": "Leadgen CRM", "hot_lead_threshold": 70}

        stage_list = [{"id": st.id, "name": st.name, "position": st.position, "is_won": st.is_won, "is_lost": st.is_lost} for st in db_client.pipeline_stages]

        return {
            "system_prompt": db_client.system_prompt or "",
            "calendly_link": db_client.calendly_link or "",
            "wa_phone_number_id": db_client.wa_phone_number_id or "",
            "pipeline_stages": stage_list,
            "brand_color": db_client.brand_color or "#10B981",
            "logo_url": db_client.logo_url or "",
            "company_display_name": db_client.company_display_name or db_client.name or "Leadgen CRM",
            "hot_lead_threshold": db_client.hot_lead_threshold if db_client.hot_lead_threshold is not None else 70,
        }

@app.patch("/api/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def update_settings(request: Request, response: Response, body: SettingsUpdateBody, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Validate hex color before any DB work (mirrors POST /api/settings/branding).
    if body.brand_color is not None and not _HEX_COLOR_RE.match(body.brand_color.strip()):
        raise HTTPException(
            status_code=400,
            detail="brand_color must be a valid hex color, e.g. '#C8A96E' or '#FFF'",
        )

    # Validate threshold is within 0-100.
    if body.hot_lead_threshold is not None and not (0 <= body.hot_lead_threshold <= 100):
        raise HTTPException(
            status_code=400,
            detail="hot_lead_threshold must be an integer between 0 and 100",
        )

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
        if body.brand_color is not None:
            db_client.brand_color = body.brand_color.strip()
        if body.logo_url is not None:
            db_client.logo_url = body.logo_url
        if body.company_display_name is not None:
            db_client.company_display_name = body.company_display_name
        if body.hot_lead_threshold is not None:
            db_client.hot_lead_threshold = body.hot_lead_threshold

        if body.pipeline_stages is not None:
            stage_map = {st.id: st for st in db_client.pipeline_stages}
            for stage_update in body.pipeline_stages:
                if stage_update.id in stage_map:
                    stage = stage_map[stage_update.id]
                    stage.name = stage_update.name
                    stage.is_won = stage_update.is_won
                    stage.is_lost = stage_update.is_lost

        s.commit()

    return {"success": True}

@app.get("/api/pipeline-stages")
@limiter.limit("120/minute", key_func=get_client_key)
def get_pipeline_stages(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Dedicated read endpoint for the tenant's ordered pipeline stages.

    Frontend `useStages()` reads from here (previously it piggy-backed on
    GET /api/settings). Same row shape as the `pipeline_stages` block that
    /api/settings returns, ordered by position.
    """
    if not SessionLocal:
        return {"pipeline_stages": []}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            return {"pipeline_stages": []}
        stage_list = [
            {"id": st.id, "name": st.name, "position": st.position, "is_won": st.is_won, "is_lost": st.is_lost}
            for st in db_client.pipeline_stages
        ]
        return {"pipeline_stages": stage_list}

# ── Sprint 9: White-label branding endpoints ───────────────────────────────

# Accepts #RGB or #RRGGBB (case-insensitive). Anchored so no extra chars slip in.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

class BrandingUpdateBody(BaseModel):
    brand_color: str | None = None
    logo_url: str | None = None
    company_display_name: str | None = None


@app.get("/api/settings/branding")
@limiter.limit("120/minute", key_func=get_client_key)
def get_branding(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Return the tenant's white-label branding fields (theme customization)."""
    if not SessionLocal:
        return {"brand_color": "#C8A96E", "logo_url": "", "company_display_name": "Leadgen CRM"}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")
        return {
            "brand_color": db_client.brand_color or "#C8A96E",
            "logo_url": db_client.logo_url or "",
            "company_display_name": db_client.company_display_name or db_client.name or "Leadgen CRM",
        }


@app.post("/api/settings/branding")
@limiter.limit("60/minute", key_func=get_client_key)
def update_branding(request: Request, response: Response, body: BrandingUpdateBody, client: Client = Depends(require_api_key)):
    """
    Update the tenant's white-label branding. All fields optional (partial
    update). brand_color, when supplied, must be a valid #RGB / #RRGGBB hex
    string or the request is rejected 400.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # ── Validate hex color before touching the DB ─────────────────────
    if body.brand_color is not None:
        color = body.brand_color.strip()
        if not _HEX_COLOR_RE.match(color):
            raise HTTPException(
                status_code=400,
                detail="brand_color must be a valid hex color, e.g. '#C8A96E' or '#FFF'",
            )
    else:
        color = None

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        if color is not None:
            db_client.brand_color = color
        if body.logo_url is not None:
            db_client.logo_url = body.logo_url
        if body.company_display_name is not None:
            db_client.company_display_name = body.company_display_name

        s.commit()

        return {
            "success": True,
            "brand_color": db_client.brand_color or "#C8A96E",
            "logo_url": db_client.logo_url or "",
            "company_display_name": db_client.company_display_name or db_client.name or "Leadgen CRM",
        }

# ── API key rotation ──────────────────────────────────────────────────────

@app.post("/api/settings/regenerate-api-key")
@limiter.limit("3/minute", key_func=get_client_key)
def regenerate_api_key(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    """
    Rotate the authenticated tenant's dashboard API key.

    The caller is already authenticated via require_api_key (Bearer token in
    the Authorization header), which is sufficient proof of key ownership —
    no second factor is required for a beta product.

    Key generation reuses the identical pattern used in onboard_client.py and
    the admin /admin/create-client endpoint:
        raw_key = secrets.token_hex(32)   # 256 bits of CSPRNG entropy
        key_hash = sha256(raw_key)        # stored; never the raw value

    The raw key is returned in the response body EXACTLY ONCE and is never
    written to any log. After this call the old key is immediately invalid.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # ── Generate new key (same CSPRNG pattern as onboard_client.py) ──────
    new_raw_key = secrets.token_hex(32)
    new_key_hash = hashlib.sha256(new_raw_key.encode("utf-8")).hexdigest()

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        db_client.dashboard_api_key_hash = new_key_hash
        s.commit()

    # Log the rotation event — client_id and timestamp only, never the key.
    logger.info(
        "API key rotated: client_id=%s at=%s",
        client.id,
        datetime.utcnow().isoformat(),
    )

    # Return the raw key once. The caller must copy it immediately.
    return {
        "success": True,
        "api_key": new_raw_key,
        "note": "Copy this key now — it will not be shown again.",
    }


# ── Template library endpoints ────────────────────────────────────────────

@app.get("/api/templates", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def list_templates(request: Request, response: Response):
    if not SessionLocal:
        return []
    with SessionLocal() as s:
        templates = s.query(PromptTemplate).order_by(PromptTemplate.id).all()
        return [
            {"slug": t.slug, "niche": t.niche, "display_name": t.display_name, "is_default": t.is_default}
            for t in templates
        ]

@app.get("/api/templates/{slug}", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def get_template(request: Request, response: Response, slug: str, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    with SessionLocal() as s:
        template = s.query(PromptTemplate).filter(PromptTemplate.slug == slug).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        body = template.body
        body = body.replace("{{agency_name}}", client.company_display_name or client.name or "Our Agency")
        body = body.replace("{{calendly_link}}", client.calendly_link or "")
        return {"slug": template.slug, "niche": template.niche, "display_name": template.display_name, "body": body}

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


@app.post("/api/admin/clients", dependencies=[Depends(require_admin_secret), Depends(require_admin)])
@limiter.limit("10/minute", key_func=get_admin_key)
def admin_create_client(request: Request, response: Response, body: AdminCreateClientBody):
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
            plan_tier="base",
            subscription_status="inactive",
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
        "plan_tier": "base",
        "subscription_status": "inactive",
    }


# ── Dashboard helper utilities ─────────────────────────────────────────────

DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

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
        "stage":         fields.get("Status", "New Lead"),
        "score":         score,
        "created_at":    created_str,
        "last_activity": last_activity,
        "last_message":  last_message_preview,
    }
# ── Dashboard endpoints ───────────────────────────────────────────────────

@app.get("/api/stats/dashboard")
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


@app.get("/api/leads")
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

    return [_format_lead_row(r) for r in records]


@app.get("/api/leads/{lead_id}")
@limiter.limit("120/minute", key_func=get_client_key)
def get_lead_detail(request: Request, response: Response, lead_id: str, client: Client = Depends(require_api_key)):
    """Return a single lead with full conversation history."""
    try:
        parsed_id = int(lead_id) if lead_id.isdigit() else lead_id
        record = store.get_lead_by_id(parsed_id, client_id=client.id)
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

@app.get("/api/leads/{lead_id}/messages")
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
    try:
        # lead_id may arrive as a Postgres integer ID or an Airtable string ID.
        try:
            parsed_id = int(lead_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid lead ID — must be a numeric Postgres ID")

        if not SessionLocal:
            # Postgres not configured; no messages can exist.
            return []

        from models import Lead, Message
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch messages for lead {lead_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.patch("/api/leads/{lead_id}/stage", dependencies=[Depends(require_api_key)])
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

@app.post("/api/leads/{lead_id}/takeover", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def takeover_lead(request: Request, response: Response, lead_id: int, client: Client = Depends(require_api_key)):
    """Pause AI for this lead — human takes over the conversation."""
    from models import Lead
    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == lead_id, Lead.client_id == client.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.is_human_takeover = True
        s.commit()
    return {"success": True, "lead_id": lead_id, "is_human_takeover": True}

@app.post("/api/leads/{lead_id}/release", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def release_lead(request: Request, response: Response, lead_id: int, client: Client = Depends(require_api_key)):
    """Resume AI for this lead — end human takeover."""
    from models import Lead
    with SessionLocal() as s:
        lead = s.query(Lead).filter(Lead.id == lead_id, Lead.client_id == client.id).first()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.is_human_takeover = False
        s.commit()
    return {"success": True, "lead_id": lead_id, "is_human_takeover": False}

class SendMessageBody(BaseModel):
    message: str

@app.post("/api/leads/{lead_id}/send-message", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def send_human_message(request: Request, response: Response, lead_id: int, body: SendMessageBody, client: Client = Depends(require_api_key)):
    """Send a manual WhatsApp message to the lead."""
    from store import get_primary_store
    store = get_primary_store()
    
    lead = store.get_lead_by_id(lead_id, client.id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
        
    wa_client = WhatsAppClient()
    try:
        wa_client.send_message(lead["phone"], body.message)
    except Exception as e:
        logger.error(f"Failed to send manual message: {e}")
        raise HTTPException(status_code=500, detail="Failed to send WhatsApp message")
        
    store.save_message(
        lead_id=lead_id,
        direction="OUTBOUND",
        body=body.message,
        msg_type="human",
    )
    return {"success": True}



# ── Sprint 8: Agency sub-account endpoints ─────────────────────────────────

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


@app.post("/api/agency/sub-accounts")
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


@app.get("/api/agency/sub-accounts")
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


@app.get("/api/agency/analytics")
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
    from models import Client as ClientModel, DailyStat

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

@app.post("/api/documents/upload", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute", key_func=get_client_key)
def upload_document(request: Request, response: Response, file: UploadFile, client: Client = Depends(require_api_key)):
    """Upload a PDF or TXT file to the tenant's knowledge base."""
    from usage import check_limit
    plan = client.plan_tier or "base"
    allowed, reason = check_limit(client.id, "document_upload", plan=plan)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")

    fname = file.filename or "upload"
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

    if ext not in ("pdf", "txt"):
        raise HTTPException(status_code=400, detail="Only PDF and TXT files are supported.")

    raw = file.file.read(MAX_FILE_SIZE + 1)
    if len(raw) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")

    if ext == "pdf":
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(raw))
        text_content = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        text_content = raw.decode("utf-8", errors="replace")

    if not text_content.strip():
        raise HTTPException(status_code=400, detail="Could not extract any text from file.")

    from ingestion import ingest_document
    stored = ingest_document(client.id, fname, text_content)
    return {"success": True, "filename": fname, "chunks_stored": stored}


@app.get("/api/documents", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def list_documents(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """List distinct documents uploaded by this tenant."""
    from models import Document as DocModel
    from sqlalchemy import func
    with SessionLocal() as s:
        rows = (
            s.query(DocModel.filename, func.count(DocModel.id), func.min(DocModel.created_at))
            .filter(DocModel.client_id == client.id)
            .group_by(DocModel.filename)
            .order_by(func.min(DocModel.created_at).desc())
            .all()
        )
        return [
            {"filename": r[0], "chunks": r[1], "uploaded_at": r[2].isoformat() if r[2] else None}
            for r in rows
        ]


# ── Billing endpoints ──────────────────────────────────────────────────────

@app.post("/api/billing/checkout", dependencies=[Depends(require_api_key)])
@limiter.limit("10/minute", key_func=get_client_key)
def billing_checkout(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Create a Razorpay order for the client's plan upgrade."""
    from billing import create_subscription
    plan = request.query_params.get("plan", "base")
    try:
        result = create_subscription(client.id, plan)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post("/api/billing/webhook")
@limiter.limit("100/minute")
async def billing_webhook(request: Request, response: Response):
    """Receive and verify Razorpay webhook events."""
    from billing import verify_webhook_signature, handle_webhook

    signature = request.headers.get("X-Razorpay-Signature", "")
    body_bytes = await request.body()

    if not verify_webhook_signature(body_bytes, signature):
        logger.warning("Invalid Razorpay webhook signature rejected.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    import json
    try:
        event_data = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    result = handle_webhook(event_data)
    return {"status": "ok", "result": result}


@app.get("/api/billing/status", dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute", key_func=get_client_key)
def billing_status(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Return the client's current billing status and monthly usage summary."""
    from usage import get_monthly_usage, PLAN_LIMITS, DEFAULT_PLAN

    plan = client.plan_tier or "base"
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN])
    usage = get_monthly_usage(client.id)

    return {
        "plan_tier": plan,
        "subscription_status": client.subscription_status or "inactive",
        "usage": usage,
        "limits": limits,
    }


def verify_signature(payload: bytes, signature_header: str) -> bool:
    """Verify Meta's X-Hub-Signature-256 header."""
    if not signature_header:
        return False
    expected_sig = hmac.new(
        WHATSAPP_APP_SECRET.encode('utf-8'),
        msg=payload,
        digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(f"sha256={expected_sig}", signature_header)


@app.get("/")
@limiter.limit("60/minute")
def read_root(request: Request, response: Response):
    return {"status": "ok", "message": "WhatsApp Acquisition System is running."}


@app.get("/health")
@limiter.limit("60/minute")
def health_check(request: Request, response: Response):
    """Infrastructure health check — no auth required."""
    db_ok = False
    redis_ok = False

    if SessionLocal:
        try:
            with SessionLocal() as s:
                s.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            pass

    if redis_conn:
        try:
            redis_conn.ping()
            redis_ok = True
        except Exception:
            pass

    status = "ok" if db_ok else "degraded"
    return {"status": status, "db": db_ok, "redis": redis_ok}


@app.get("/webhook")
@limiter.limit("10/minute")
def verify_webhook(request: Request, response: Response):
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
    calendly_link: str | None,
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

    store = get_store()
    req_gemini = GeminiClient(system_prompt=system_prompt, calendly_link=calendly_link)
    
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
async def receive_message(request: Request, response: Response, background_tasks: BackgroundTasks):
    """
    Receive incoming messages from WhatsApp users.
    Fast-ACK: HMAC verify → dedup → enqueue RQ job → return 200.
    All LLM calls, store operations, and WhatsApp sends happen in the worker.
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
                phone_number_id = value.get("metadata", {}).get("phone_number_id")

                if "messages" in value:
                    for message in value["messages"]:
                        msg_id = message.get("id", "")

                        # Redis dedup: SETNX returns False if key already exists
                        if msg_id and redis_conn:
                            try:
                                redis_conn.ping()  # verify connection is alive
                                dedup_key = f"wamid:{msg_id}"
                                if not redis_conn.setnx(dedup_key, 1):
                                    logger.info(f"Duplicate webhook deduped at Redis | wamid: {msg_id}")
                                    continue
                                redis_conn.expire(dedup_key, 86400)
                            except Exception as e:
                                logger.warning(f"Redis unavailable, skipping dedup check: {e}")

                        # DB-level dedup fallback when Redis unavailable
                        if msg_id:
                            try:
                                from database import SessionLocal
                                from models import Message
                                from sqlalchemy import select
                                with SessionLocal() as session:
                                    existing = session.execute(
                                        select(Message).where(Message.wa_message_id == msg_id)
                                    ).scalar_one_or_none()
                                    if existing:
                                        logger.info(f"Duplicate webhook deduped at DB | wamid: {msg_id}")
                                        continue
                            except Exception as db_err:
                                logger.warning(f"DB dedup check failed: {db_err}")

                        # Use BackgroundTasks directly instead of RQ (since no worker is deployed yet)
                        from jobs import process_webhook_message
                        background_tasks.add_task(process_webhook_message, phone_number_id=phone_number_id, message_data=message)

                if "statuses" in value:
                    for status in value["statuses"]:
                        from jobs import process_status_update
                        background_tasks.add_task(process_status_update, status_data=status)

        return {"status": "queued"}
    return {"status": "ignored"}


@app.get("/api/analytics/summary", dependencies=[Depends(require_api_key)])
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
    from models import DailyStat

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


@app.get("/api/analytics/funnel", dependencies=[Depends(require_api_key)])
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

@app.get("/api/analytics/response-time", dependencies=[Depends(require_api_key)])
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
    from models import DailyStat

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

@app.get("/api/analytics/bookings", dependencies=[Depends(require_api_key)])
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

@app.get("/api/analytics/sources", dependencies=[Depends(require_api_key)])
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


import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.dependencies import limiter
from app.api.routers import (
    admin,
    agency,
    analytics,
    auth,
    billing,
    campaigns,
    documents,
    email,
    health,
    leads,
    settings,
    whatsapp as whatsapp_routes,
    whatsapp_observability as whatsapp_observability_routes,
    whatsapp_operations as whatsapp_operations_routes,
    whatsapp_policy as whatsapp_policy_routes,
    whatsapp_sequences as whatsapp_sequences_routes,
)
from app.api.runtime import calendly, logger, store, whatsapp
from app.core.config import (
    CLIENT_ID,
    FOLLOWUP_TEMPLATE_NAME,
    WHATSAPP_APP_SECRET,
)
from app.services import analytics as analytics_service
from app.services import tenant
from app.services import whatsapp_policy


def follow_up_job():
    """Hourly job: nudge leads stuck in 'Contacted' for >48h."""
    logger.info("Running hourly follow-up job...")
    clients = tenant.get_all_active_clients()
    if not clients:
        # Fallback for when Postgres isn't configured (airtable mode)
        logger.info("No active clients found (Postgres not configured), running in single-tenant mode.")
        _process_followups_for_client(
            client_id=CLIENT_ID,
            template_name=FOLLOWUP_TEMPLATE_NAME,
        )
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
                    result = whatsapp_policy.send_immediate_template(
                        client_id=client_id,
                        phone=phone,
                        template_name=template_name,
                        language="en",
                        sender=whatsapp.send_template,
                        action="follow_up_template_send",
                    )
                    if result.state == "sent" and result.provider_message_id:
                        store.append_message(
                            phone, direction="outbound",
                            message=f"[template: {template_name}]",
                            msg_type="template",
                            wa_message_id=result.provider_message_id,
                            client_id=client_id,
                        )
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
            
        lead = store.get_lead(phone, client_id=CLIENT_ID)
        if lead:
            matched_client_id = lead.get("fields", {}).get("client_id") or CLIENT_ID
            current_status = lead.get("fields", {}).get("Status")
            if current_status not in ("Booked", "Lost"):
                store.update_lead_status(
                    phone,
                    "Booked",
                    client_id=matched_client_id,
                )
                store.append_message(
                    phone,
                    "system",
                    f"Calendly Booking Confirmed for {booking.get('start_time')}",
                    "system",
                    client_id=matched_client_id,
                )
                
                alert = whatsapp_policy.get_operator_template(
                    client_id=matched_client_id, event="booking"
                )
                if alert:
                    try:
                        whatsapp_policy.send_immediate_template(
                            client_id=matched_client_id,
                            phone=alert.phone,
                            template_name=alert.name,
                            language=alert.language,
                            parameters=[
                                str(booking.get("name") or ""),
                                str(booking.get("start_time") or ""),
                            ],
                            recipient_kind="operator",
                            sender=whatsapp.send_template,
                            action="booking_alert_send",
                        )
                    except whatsapp_policy.WhatsAppPolicyError as exc:
                        logger.warning("Booking WhatsApp alert blocked: %s", exc)
                else:
                    logger.warning(
                        "Booking alert suppressed: tenant operator template is not configured"
                    )
            else:
                logger.info(f"Matched booking for {phone} but lead status is {current_status}, skipping update.")
        else:
            logger.info(f"Unmatched booking (phone {phone} not in Leads): {booking.get('name')}")

scheduler = BackgroundScheduler()
# Phase 8 replaces legacy web-process follow-ups. Sequence ticking is started
# and rescheduled by the dedicated RQ worker only.
scheduler.add_job(calendly_sync_job, 'interval', minutes=5)
# Nightly analytics rollup — 02:00 IST every day. Rolls up YESTERDAY (IST) for
# every active tenant. CronTrigger timezone is explicit so it fires at 2 AM IST
# regardless of the host/container timezone (Render runs UTC).
scheduler.add_job(
    analytics_service.run_nightly_rollup,
    CronTrigger(hour=2, minute=0, timezone="Asia/Kolkata"),
    id="nightly_rollup",
    replace_existing=True,
)
# Phase E7: email campaign sequence runner — due enrollments every 5 minutes
from app.email.email_campaigns import run_campaign_tick_job
scheduler.add_job(
    run_campaign_tick_job,
    "interval",
    minutes=5,
    id="email_campaign_tick",
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

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

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

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(email.router)
app.include_router(campaigns.router)
app.include_router(settings.account_router)
app.include_router(admin.router)
app.include_router(leads.router)
app.include_router(agency.router)
app.include_router(documents.router)
app.include_router(billing.router)
app.include_router(health.router)
app.include_router(whatsapp_routes.router)
app.include_router(whatsapp_observability_routes.tenant_router)
app.include_router(whatsapp_observability_routes.admin_router)
app.include_router(whatsapp_operations_routes.tenant_router)
app.include_router(whatsapp_operations_routes.admin_router)
app.include_router(whatsapp_policy_routes.router)
app.include_router(whatsapp_sequences_routes.router)
app.include_router(analytics.router)

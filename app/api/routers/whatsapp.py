import hashlib
import hmac

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from app.api.dependencies import limiter
from app.api.runtime import logger, redis_conn, whatsapp
from app.core.config import (
    WHATSAPP_APP_SECRET,
    WHATSAPP_VERIFY_TOKEN,
)

router = APIRouter()

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



@router.get("/webhook")
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
    from app.store.store import get_store
    from app.clients.gemini_client import GeminiClient

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

@router.post("/webhook")
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
                                from app.core.database import SessionLocal
                                from app.core.models import Message
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
                        from app.services.jobs import process_webhook_message
                        background_tasks.add_task(process_webhook_message, phone_number_id=phone_number_id, message_data=message)

                if "statuses" in value:
                    for status in value["statuses"]:
                        from app.services.jobs import process_status_update
                        background_tasks.add_task(process_status_update, status_data=status)

        return {"status": "queued"}
    return {"status": "ignored"}

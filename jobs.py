"""
RQ job functions for webhook processing.

These run in a separate worker process, decoupled from the HTTP request.
The webhook handler enqueues jobs here after HMAC verify + dedup check,
then returns 200 immediately to ACK Meta.
"""

import logging

from config import (
    LORD_PHONE_NUMBER, BLOCKED_NUMBERS, MIGRATION_MODE, CLIENT_ID,
)
from whatsapp_client import WhatsAppClient
from gemini_client import GeminiClient
from store import get_store
from guardrails import scan_input, redact_pii, score_confidence, CONFIDENCE_THRESHOLD
from database import SessionLocal
from models import Lead, Client
from rag import retrieve_context
from usage import log_usage, estimate_tokens, check_limit, COST_PER_1K_INPUT_TOKENS, COST_PER_1K_OUTPUT_TOKENS
import tenant

logger = logging.getLogger(__name__)

whatsapp = WhatsAppClient()


def process_webhook_message(phone_number_id: str, message_data: dict):
    """
    Process a single inbound WhatsApp message end-to-end:
    tenant resolution → lead CRUD → AI reply → send → analytics.
    """
    store = get_store()

    # ── 1. Resolve tenant context ────────────────────────────────────────
    ctx = tenant.resolve_context_by_phone_id(phone_number_id) if phone_number_id else None

    if not ctx:
        if MIGRATION_MODE == "airtable" or not tenant.is_configured():
            fallback_client = tenant.load_client(CLIENT_ID)
            req_gemini = tenant.get_gemini_for_client(fallback_client)
            req_won_stages = tenant.get_won_stage_names(CLIENT_ID)
            req_lost_stages = tenant.get_lost_stage_names(CLIENT_ID)
            current_client_id = CLIENT_ID
        else:
            logger.warning(f"Unknown phone_number_id: {phone_number_id}")
            return
    else:
        req_gemini = ctx.gemini
        req_won_stages = ctx.won_stages
        req_lost_stages = ctx.lost_stages
        current_client_id = ctx.client.id

    sender_phone = message_data.get("from")
    message_type = message_data.get("type")
    msg_id = message_data.get("id", "")

    # ── 2. LORD phone loop guard ─────────────────────────────────────────
    normalized_sender = sender_phone.replace('+', '').replace(' ', '').replace('-', '') if sender_phone else ''
    normalized_lord = LORD_PHONE_NUMBER.replace('+', '').replace(' ', '').replace('-', '') if LORD_PHONE_NUMBER else ''
    if normalized_lord and normalized_sender == normalized_lord:
        logger.warning(f"Ignored: message from LORD_PHONE_NUMBER ({sender_phone}) — loop guard triggered.")
        return

    if message_type != "text":
        return

    user_text = message_data.get("text", {}).get("body", "")
    if not user_text:
        return

    logger.info(f"[RQ] Processing message from {sender_phone}: {user_text}")

    # ── 3. Get or create lead ────────────────────────────────────────────
    lead = store.get_lead(sender_phone, client_id=current_client_id)
    if not lead:
        if sender_phone and sender_phone.lstrip('+').startswith('1555'):
            logger.info(f"Ignored Meta test number: {sender_phone}")
            return

        normalized_sender_clean = sender_phone.replace('+', '').replace(' ', '').replace('-', '') if sender_phone else ''
        blocked_clean = [n.replace('+', '').replace(' ', '').replace('-', '') for n in BLOCKED_NUMBERS]
        if normalized_sender_clean in blocked_clean:
            logger.info(f"Ignored blocked number: {sender_phone}")
            return

        # Try to extract WhatsApp profile name if available (default to Unknown)
        profile_name = message_data.get("profile_name", "Unknown")
        if "contacts" in message_data and isinstance(message_data["contacts"], list) and len(message_data["contacts"]) > 0:
            profile = message_data["contacts"][0].get("profile", {})
            profile_name = profile.get("name", profile_name)

        logger.info(f"New unknown number {sender_phone} — creating lead automatically.")
        new_record = store.add_lead(
            name=profile_name,
            phone=sender_phone,
            source="Inbound WhatsApp",
            client_id=current_client_id,
        )
        if not new_record:
            logger.error(f"Failed to create lead for {sender_phone}. Dropping message.")
            return

        lead = new_record

    # ── 4. Persistent idempotency (append inbound message) ───────────────
    appended = store.append_message(
        sender_phone, direction="inbound", message=user_text,
        msg_type="text", wa_message_id=msg_id, client_id=current_client_id,
    )
    if not appended:
        logger.info(f"Duplicate webhook skipped | wamid: {msg_id} | phone: {sender_phone}")
        return

    current_status = lead.get("fields", {}).get("Status", "New Lead")
    if current_status == "New Lead":
        store.update_lead_status(sender_phone, "Contacted", client_id=current_client_id)

    # ── 4b. Human takeover gate ──────────────────────────────────────────
    if lead.get("fields", {}).get("is_human_takeover"):
        lead_id = lead.get("id", "?")
        logger.info(f"Human takeover active for lead {lead_id} ({sender_phone}) — skipping AI response.")
        return

    # ── 4b2. Usage hard cap check ───────────────────────────────────────
    if current_client_id:
        plan = "base"
        with SessionLocal() as session:
            db_client = session.get(Client, int(client_id))
            if db_client and db_client.plan_tier:
                plan = db_client.plan_tier
        allowed, reason = check_limit(current_client_id, "ai_response", plan=plan)
        if not allowed:
            logger.warning(f"AI cap hit for client {current_client_id}, lead {sender_phone}: {reason}")
            lead_id_int = int(lead.get("id", 0))
            with SessionLocal() as session:
                db_lead = session.get(Lead, lead_id_int)
                if db_lead:
                    db_lead.is_human_takeover = True
                    session.commit()
            return

    # ── 4c. Input guardrails ─────────────────────────────────────────────
    is_safe, refusal = scan_input(user_text)
    if not is_safe:
        logger.warning(f"Prompt injection blocked for {sender_phone}: sending safe refusal.")
        wamid = whatsapp.send_message(sender_phone, refusal)
        store.append_message(
            sender_phone, direction="outbound", message=refusal,
            msg_type="text", wa_message_id=wamid, client_id=current_client_id,
        )
        return

    llm_text = redact_pii(user_text)

    # ── 4d. RAG context retrieval ────────────────────────────────────────
    rag_context = ""
    if current_client_id:
        rag_chunks = retrieve_context(current_client_id, llm_text)
        if rag_chunks:
            rag_context = "\n\n---\nKNOWLEDGE BASE (use this to answer the customer):\n"
            rag_context += "\n---\n".join(rag_chunks)
            rag_context += "\n---\n"
            logger.info(f"RAG injected {len(rag_chunks)} chunks for {sender_phone}.")

    # ── 5. Generate AI reply ─────────────────────────────────────────────
    last_message = lead.get("fields", {}).get("Last_Message", "")
    updated_last_message = last_message + f"\n[INBOUND - text]\n{user_text}\n"

    if rag_context:
        original_prompt = req_gemini._system_prompt
        req_gemini._system_prompt = original_prompt + rag_context

    parsed_history = req_gemini.parse_conversation_history(updated_last_message)
    ai_reply = req_gemini.generate_response_with_history(parsed_history, llm_text)

    if rag_context:
        req_gemini._system_prompt = original_prompt

    if not ai_reply or not ai_reply.strip():
        logger.error(f"AI returned empty reply for {sender_phone}. Sending fallback.")
        ai_reply = "Sorry, abhi network issue hai. Main thodi der mein aapse connect karta hu."
    elif len(ai_reply) > 4096:
        logger.error(f"AI reply too long ({len(ai_reply)} chars) for {sender_phone}. Truncating.")
        ai_reply = ai_reply[:4093] + "..."

    # ── 5b. Output guardrail — confidence scoring ────────────────────────
    system_prompt = getattr(req_gemini, "_system_prompt", None)
    confidence = score_confidence(ai_reply, system_prompt)
    if confidence < CONFIDENCE_THRESHOLD:
        lead_id_int = int(lead.get("id", 0))
        logger.warning(
            f"Low confidence ({confidence:.2f}) for lead {lead_id_int} ({sender_phone}) "
            f"— triggering human takeover, AI reply withheld."
        )
        with SessionLocal() as session:
            db_lead = session.get(Lead, lead_id_int)
            if db_lead:
                db_lead.is_human_takeover = True
                session.commit()
        return

    # ── 6. Send WhatsApp reply ───────────────────────────────────────────
    wamid = whatsapp.send_message(sender_phone, ai_reply)
    store.append_message(
        sender_phone, direction="outbound", message=ai_reply,
        msg_type="text", wa_message_id=wamid, client_id=current_client_id,
    )

    updated_last_message += f"\n[OUTBOUND - text]\n{ai_reply}\n"

    # ── 6b. Log AI usage ────────────────────────────────────────────────
    if current_client_id:
        input_tokens = estimate_tokens(llm_text)
        output_tokens = estimate_tokens(ai_reply)
        total_tokens = input_tokens + output_tokens
        cost = (input_tokens / 1000) * COST_PER_1K_INPUT_TOKENS + (output_tokens / 1000) * COST_PER_1K_OUTPUT_TOKENS
        log_usage(current_client_id, "ai_response", total_tokens, round(cost, 6))

    # ── 7. Analytics & extraction (inline — already off the HTTP path) ───
    lead_name = lead.get("fields", {}).get("Name", "Unknown") if isinstance(lead, dict) else lead.business_name

    _run_analytics(
        store, sender_phone, updated_last_message, user_text,
        lead_name, req_gemini, req_won_stages, req_lost_stages, current_client_id,
    )


def process_status_update(status_data: dict, current_client_id: int = None):
    """Process a WhatsApp message status update (delivered/read)."""
    store = get_store()
    wamid = status_data["id"]
    status_str = status_data["status"]
    logger.info(f"[RQ] Message {wamid} status: {status_str}")
    store.update_message_status(wamid, status_str, client_id=current_client_id)


def _run_analytics(
    store, sender_phone, updated_last_message, user_text,
    lead_name, req_gemini, req_won_stages, req_lost_stages, current_client_id,
):
    """
    Lead scoring, info extraction, status updates, lord notification.
    Mirrors _process_analytics_and_extraction_bg from main.py but runs
    inline inside the RQ worker (already off the HTTP hot path).
    """
    score = None

    try:
        score = req_gemini.score_lead(updated_last_message)
        store.update_lead_score(sender_phone, score, client_id=current_client_id)
    except Exception as e:
        logger.error(f"Lead scoring failed: {e}")

    try:
        info = req_gemini.extract_lead_info(updated_last_message)
        if info:
            store.update_lead_info(
                sender_phone,
                name=info.get("Name"),
                business_name=info.get("Business_Name"),
                client_id=current_client_id,
            )
    except Exception as e:
        logger.error(f"Lead info extraction failed: {e}")

    try:
        if score in req_won_stages:
            store.update_lead_status(sender_phone, "Qualified", client_id=current_client_id)
        elif score == "Cold":
            decline_keywords = ["not interested", "stop", "no", "nahi", "cancel", "unsubscribe"]
            if any(word in user_text.lower() for word in decline_keywords):
                lost_stage = req_lost_stages[0] if req_lost_stages else "Lost"
                store.update_lead_status(sender_phone, lost_stage, client_id=current_client_id)
                logger.info(f"Lead {sender_phone} marked as {lost_stage} due to explicit decline.")
    except Exception as e:
        logger.error(f"Status update failed: {e}")

    try:
        if score in req_won_stages:
            lord_phone = LORD_PHONE_NUMBER
            if lord_phone:
                norm_lord = lord_phone.replace('+', '').replace(' ', '').replace('-', '')
                if store.get_lead(norm_lord, client_id=current_client_id):
                    logger.error(
                        f"ALERT SUPPRESSED: LORD_PHONE_NUMBER ({lord_phone}) matches an "
                        f"existing lead record. Update LORD_PHONE_NUMBER in .env to avoid loop."
                    )
                else:
                    whatsapp.send_message(lord_phone, f"🔥 HOT LEAD ALERT: Check Airtable for {lead_name} ({sender_phone})")
            else:
                logger.info(f"🔥 HOT LEAD: {lead_name} {sender_phone}")
    except Exception as e:
        logger.error(f"Lord notification failed: {e}")

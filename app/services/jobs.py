"""
RQ job functions for webhook processing.

These run in a separate worker process, decoupled from the HTTP request.
The webhook handler enqueues jobs here after HMAC verify + dedup check,
then returns 200 immediately to ACK Meta.
"""

import logging
from datetime import datetime

from app.core.config import (
    LORD_PHONE_NUMBER, BLOCKED_NUMBERS, CLIENT_ID,
    WHATSAPP_LOCAL_TEST_TENANT_FALLBACK,
)
from app.clients.whatsapp_client import WhatsAppClient
from app.store.store import get_store
from app.core import database
from app.core.models import Lead, Client
from app.services.usage import log_usage, estimate_tokens, check_limit, COST_PER_1K_INPUT_TOKENS, COST_PER_1K_OUTPUT_TOKENS
from app.services import tenant
from app.services import whatsapp_outbox
from app.services import whatsapp_policy
from app.services import ai_decision
from app.services.guardrails import minimize_sensitive_text, scan_input

logger = logging.getLogger(__name__)

whatsapp = WhatsAppClient()


def process_webhook_message(
    phone_number_id: str,
    message_data: dict,
    current_client_id: int | None = None,
    inbound_event_id: str | None = None,
    correlation_id: str | None = None,
):
    """
    Process a single inbound WhatsApp message end-to-end:
    tenant resolution → lead CRUD → AI reply → send → analytics.
    """
    # ── 1. Resolve tenant context ────────────────────────────────────────
    ctx = tenant.resolve_context_by_phone_id(phone_number_id) if phone_number_id else None

    if current_client_id is not None:
        # The webhook resolved this immutable tenant ID at ingress. Re-resolve
        # the phone ID here and reject a mismatch rather than trusting a stale
        # or forged task payload.
        if not ctx or ctx.client.id != current_client_id:
            logger.warning("Webhook job skipped due to tenant context mismatch")
            return
    if not ctx:
        if WHATSAPP_LOCAL_TEST_TENANT_FALLBACK:
            fallback_client = tenant.load_client(CLIENT_ID)
            req_gemini = tenant.get_gemini_for_client(fallback_client)
            req_won_stages = tenant.get_won_stage_names(CLIENT_ID)
            req_lost_stages = tenant.get_lost_stage_names(CLIENT_ID)
            current_client_id = CLIENT_ID
        else:
            logger.warning("Webhook job skipped without a verified tenant context")
            return
    else:
        req_gemini = ctx.gemini
        req_won_stages = ctx.won_stages
        req_lost_stages = ctx.lost_stages
        current_client_id = ctx.client.id

    store = get_store()

    sender_phone = message_data.get("from")
    message_type = message_data.get("type")
    msg_id = message_data.get("id", "")

    if not isinstance(sender_phone, str) or not sender_phone.strip():
        logger.warning("WhatsApp message skipped without a valid sender")
        return

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

    logger.info("[RQ] Processing tenant-scoped WhatsApp message", extra={"client_id": current_client_id, "event_id": msg_id})

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

    # Phase 7: STOP/decline intent becomes durable before stage updates,
    # takeover checks, usage checks, guardrails, or AI generation.
    if whatsapp_outbox.record_inbound_opt_out(
        client_id=current_client_id,
        recipient_phone=sender_phone,
        text=user_text,
        inbound_event_id=inbound_event_id,
    ):
        lost_stage = req_lost_stages[0] if req_lost_stages else "Lost"
        store.update_lead_status(
            sender_phone,
            lost_stage,
            client_id=current_client_id,
        )
        logger.info("Durable WhatsApp opt-out recorded; automated reply suppressed")
        return

    preflight = whatsapp_policy.preflight_text(
        client_id=current_client_id,
        phone=sender_phone,
        correlation_id=correlation_id,
    )
    if not preflight.allowed:
        logger.info("WhatsApp reply suppressed by preflight policy: %s", preflight.reason_code)
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
        session_factory = database.SessionLocal
        if session_factory is not None:
            with session_factory() as session:
                db_client = session.get(Client, int(current_client_id))
                if db_client and db_client.plan_tier:
                    plan = db_client.plan_tier
        allowed, reason = check_limit(current_client_id, "ai_response", plan=plan)
        if not allowed:
            logger.warning(f"AI cap hit for client {current_client_id}, lead {sender_phone}: {reason}")
            store.update_human_takeover_by_id(
                lead["id"],
                True,
                client_id=current_client_id,
            )
            return

    # Phase 9: the durable intent is claimed only after all pre-AI Phase 7
    # policy gates and takeover checks. Every later send repeats them under lock.
    intent_id = None
    if inbound_event_id is not None:
        intent_id = whatsapp_outbox.create_or_get_intent(
            client_id=current_client_id, inbound_provider_event_id=inbound_event_id,
            recipient_phone=sender_phone, correlation_id=correlation_id,
        )
        claim = whatsapp_outbox.claim_for_generation(intent_id=intent_id, client_id=current_client_id)
        if claim == "dispatch":
            whatsapp_outbox.process_outbound_intent(intent_id=intent_id, client_id=current_client_id)
            return
        if claim != "generate":
            return

    if database.SessionLocal is None:
        return
    with database.SessionLocal() as session:
        durable_lead = session.query(Lead).filter_by(client_id=current_client_id, phone=sender_phone).one_or_none()
    if durable_lead is None:
        logger.error("AI reply withheld: tenant lead has no durable row")
        return
    if not scan_input(user_text)[0]:
        result = ai_decision.reject_input(client_id=current_client_id, lead_id=durable_lead.id, correlation_id=correlation_id, reason="prompt_injection", intent_id=intent_id)
    else:
        result = ai_decision.evaluate(client_id=current_client_id, lead_id=durable_lead.id, inbound_text=user_text, gemini=req_gemini, correlation_id=correlation_id, intent_id=intent_id)
    if result.value.decision != "REPLY":
        if intent_id is not None:
            ai_decision.finalize_non_reply(
                intent_id=intent_id,
                client_id=current_client_id,
                lead_id=durable_lead.id,
                result=result,
            )
        else:
            ai_decision.record_outcome(client_id=current_client_id, lead_id=durable_lead.id, result=result, outcome="escalated" if result.value.decision == "ESCALATE" else result.value.decision.lower())
        if result.value.decision == "ESCALATE":
            store.update_human_takeover_by_id(lead["id"], True, client_id=current_client_id)
        return
    if intent_id is None:
        # Webhook jobs always supply an event id; retain fail-closed behavior for legacy callers.
        ai_decision.record_outcome(client_id=current_client_id, lead_id=durable_lead.id, result=result, outcome="no_durable_intent")
        return
    try:
        ai_decision.queue_reply(intent_id=intent_id, client_id=current_client_id, lead_id=durable_lead.id, result=result)
        dispatch = whatsapp_outbox.dispatch_intent(intent_id=intent_id, client_id=current_client_id, sender=whatsapp.send_message, final_guard=ai_decision.final_send_guard(result, intent_id))
    except Exception:
        ai_decision.record_outcome(client_id=current_client_id, lead_id=durable_lead.id, result=result, outcome="failed")
        raise
    ai_decision.record_outcome(client_id=current_client_id, lead_id=durable_lead.id, result=result, outcome=dispatch.state)
    if dispatch.state != "sent":
        return
    updated_last_message = result.context_text

    # ── 6b. Log AI usage ────────────────────────────────────────────────
    if current_client_id:
        input_tokens = result.token_estimate
        output_tokens = estimate_tokens(result.rendered_text)
        total_tokens = input_tokens + output_tokens
        cost = (input_tokens / 1000) * COST_PER_1K_INPUT_TOKENS + (output_tokens / 1000) * COST_PER_1K_OUTPUT_TOKENS
        log_usage(current_client_id, "ai_response", total_tokens, round(cost, 6))

    # ── 7. Analytics & extraction (inline — already off the HTTP path) ───
    lead_name = lead.get("fields", {}).get("Name", "Unknown") if isinstance(lead, dict) else lead.business_name

    _run_analytics(
        store, sender_phone, updated_last_message, minimize_sensitive_text(user_text, known_names=[lead_name]),
        lead_name, req_gemini, req_won_stages, req_lost_stages, current_client_id,
    )


def process_status_update(
    status_data: dict,
    current_client_id: int | None = None,
    phone_number_id: str | None = None,
    require_known_intent: bool = False,
):
    """Process a WhatsApp message status update (delivered/read)."""
    ctx = tenant.resolve_context_by_phone_id(phone_number_id) if phone_number_id else None

    if current_client_id is not None:
        if not ctx or ctx.client.id != current_client_id:
            logger.warning("Status update skipped due to tenant context mismatch")
            return
    elif ctx:
        current_client_id = ctx.client.id
    elif WHATSAPP_LOCAL_TEST_TENANT_FALLBACK:
        current_client_id = CLIENT_ID

    if current_client_id is None:
        logger.warning(
            "WhatsApp status update skipped without tenant context",
            extra={"event": "status_update_missing_tenant"},
        )
        return

    store = get_store()
    wamid = status_data["id"]
    status_str = status_data["status"]
    logger.info(f"[RQ] Message {wamid} status: {status_str}")
    if require_known_intent and not whatsapp_outbox.apply_provider_status(
        client_id=current_client_id, provider_message_id=wamid, status=status_str,
    ):
        logger.warning("Rejected unknown or cross-tenant WhatsApp provider status", extra={"event": "unknown_provider_status"})
        return
    store.update_message_status(wamid, status_str, client_id=current_client_id)


def _run_analytics(
    store, sender_phone, updated_last_message, user_text,
    lead_name, req_gemini, req_won_stages, req_lost_stages, current_client_id,
):
    """
    Lead scoring, info extraction, status updates, lord notification.
    """
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
        score_data = req_gemini.score_lead(updated_last_message)
        numeric_score = score_data.get("score", 0)
        
        # Calculate derived string score based on threshold
        session_factory = database.SessionLocal
        if session_factory is None:
            logger.debug("Postgres analytics skipped because the database is not configured")
            return

        with session_factory() as session:
            client = session.query(Client).filter(Client.id == current_client_id).first()
            lead = session.query(Lead).filter(Lead.phone == sender_phone, Lead.client_id == current_client_id).first()
            
            if client and lead:
                threshold = client.hot_lead_threshold
                if numeric_score >= threshold:
                    string_score = "Hot"
                elif numeric_score >= (threshold * 0.5):
                    string_score = "Warm"
                else:
                    string_score = "Cold"
                
                # Save to database
                lead.lead_score_numeric = numeric_score
                lead.lead_score = string_score
                
                # Check for alert
                if string_score == "Hot" and not lead.notified_hot_at:
                    alert = whatsapp_policy.get_operator_template(
                        client_id=current_client_id, event="hot_lead"
                    )
                    if alert:
                        result = whatsapp_policy.send_immediate_template(
                            client_id=current_client_id,
                            phone=alert.phone,
                            template_name=alert.name,
                            language=alert.language,
                            parameters=[lead_name, sender_phone, str(numeric_score)],
                            recipient_kind="operator",
                            sender=whatsapp.send_template,
                            action="hot_lead_alert_send",
                        )
                        if result.state == "sent":
                            lead.notified_hot_at = datetime.utcnow()
                    else:
                        logger.warning(
                            "Hot-lead alert suppressed: tenant operator template is not configured"
                        )

                session.commit()
                
                # Explicit decline logic
                if string_score == "Cold":
                    if whatsapp_policy.is_opt_out_text(user_text):
                        lost_stage = req_lost_stages[0] if req_lost_stages else "Lost"
                        lead.status = lost_stage
                        session.commit()
                        logger.info(f"Lead {sender_phone} marked as {lost_stage} due to explicit decline.")

    except Exception as e:
        logger.error(f"Analytics/Scoring process failed: {e}")

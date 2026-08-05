import hashlib
import hmac
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.dependencies import limiter
from app.api.runtime import logger, whatsapp
from app.core.config import (
    WHATSAPP_APP_SECRET,
    WHATSAPP_VERIFY_TOKEN,
)
from app.services import tenant
from app.services import whatsapp_policy

router = APIRouter()

def verify_signature(payload: bytes, signature_header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header."""
    if not WHATSAPP_APP_SECRET or not signature_header:
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
        if mode != "subscribe" or token != WHATSAPP_VERIFY_TOKEN:
            raise HTTPException(status_code=403, detail="Verification token mismatch")
        if challenge is None:
            raise HTTPException(status_code=400, detail="Missing webhook challenge")
        try:
            parsed_challenge = int(challenge)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid webhook challenge")
        logger.info("Webhook verified successfully.")
        return parsed_challenge

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
    current_client_id: int,
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
        store.update_lead_score(
            sender_phone,
            score,
            client_id=current_client_id,
        )
    except Exception as e:
        logger.error("background_lead_scoring_failed", extra={"error_type": type(e).__name__})

    # 2. Information Extraction (Independent Try/Except)
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
        logger.error("background_lead_extraction_failed", extra={"error_type": type(e).__name__})

    # 3. Status Updates (Independent Try/Except)
    try:
        if score in req_won_stages:
            store.update_lead_status(
                sender_phone,
                "Qualified",
                client_id=current_client_id,
            )
        elif score == "Cold":
            if whatsapp_policy.is_opt_out_text(user_text):
                lost_stage = req_lost_stages[0] if req_lost_stages else "Lost"
                store.update_lead_status(
                    sender_phone,
                    lost_stage,
                    client_id=current_client_id,
                )
                logger.info(f"Lead {sender_phone} marked as {lost_stage} due to explicit decline.")
    except Exception as e:
        logger.error("background_status_update_failed", extra={"error_type": type(e).__name__})

    # 4. Lord Notification (Executed last, constraint #4)
    try:
        if score in req_won_stages:
            alert = whatsapp_policy.get_operator_template(
                client_id=current_client_id, event="hot_lead"
            )
            if alert:
                whatsapp_policy.send_immediate_template(
                    client_id=current_client_id,
                    phone=alert.phone,
                    template_name=alert.name,
                    language=alert.language,
                    parameters=[lead_name, sender_phone, str(score)],
                    recipient_kind="operator",
                    sender=whatsapp.send_template,
                    action="legacy_hot_lead_alert_send",
                )
            else:
                logger.warning(
                    "Legacy hot-lead alert suppressed: tenant operator template is not configured"
                )
    except Exception as e:
        logger.error("background_operator_notification_failed", extra={"error_type": type(e).__name__})

@router.post("/webhook")
@limiter.limit("1000/minute")
async def receive_message(request: Request, response: Response):
    """
    Receive incoming messages from WhatsApp users.
    Fast-ACK: HMAC verify → dedup → enqueue RQ job → return 200.
    All LLM calls, store operations, and WhatsApp sends happen in the worker.
    """
    started_at = perf_counter()
    correlation_ids: list[str] = []
    observed_client_ids: set[int] = set()
    # 1. Verify signature
    signature = request.headers.get("X-Hub-Signature-256")
    body_bytes = await request.body()
    if not verify_signature(body_bytes, signature):
        logger.warning("Invalid webhook signature rejected.")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload")

    if body.get("object") == "whatsapp_business_account":
        if not isinstance(body.get("entry", []), list):
            raise HTTPException(status_code=400, detail="Invalid webhook entries")
        for entry in body.get("entry", []):
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail="Invalid webhook entry")
            for change in entry.get("changes", []):
                if not isinstance(change, dict):
                    raise HTTPException(status_code=400, detail="Invalid webhook change")
                value = change.get("value", {})
                if not isinstance(value, dict):
                    raise HTTPException(status_code=400, detail="Invalid webhook value")
                phone_number_id = value.get("metadata", {}).get("phone_number_id")

                # Tenant routing is an ingress invariant: never let an
                # unknown/inactive phone number reach deduplication, CRM, AI,
                # provider, or background processing in dual/postgres mode.
                # Airtable-only local deployments are the sole documented
                # compatibility mode without database-backed phone routing.
                tenant_context = tenant.resolve_context_by_phone_id(phone_number_id) if phone_number_id else None
                if tenant_context is None:
                    logger.warning(
                        "WhatsApp webhook skipped for unknown or inactive phone_number_id",
                        extra={"event": "unknown_tenant_phone_number"},
                    )
                    # Do not ACK as queued: this event has neither a durable
                    # receipt nor a queue job. Meta can surface/retry the
                    # configuration error instead of the event being lost.
                    raise HTTPException(
                        status_code=403,
                        detail="Unknown or inactive WhatsApp phone number",
                    )

                current_client_id = tenant_context.client.id
                observed_client_ids.add(current_client_id)

                if "messages" in value:
                    for message in value["messages"]:
                        if not isinstance(message, dict):
                            raise HTTPException(status_code=400, detail="Invalid WhatsApp message")
                        # The envelope contains only the provider event needed by
                        # the worker; contacts are reduced to the profile name.
                        job_payload = dict(message)
                        contacts = value.get("contacts")
                        if isinstance(contacts, list) and contacts:
                            profile = contacts[0].get("profile", {}) if isinstance(contacts[0], dict) else {}
                            if isinstance(profile, dict) and profile.get("name"):
                                job_payload["profile_name"] = profile["name"]
                        correlation_ids.append(_enqueue_or_retry(
                            kind="message",
                            payload=job_payload,
                            phone_number_id=phone_number_id,
                            client_id=current_client_id,
                        ))

                if "statuses" in value:
                    for status in value["statuses"]:
                        if not isinstance(status, dict):
                            raise HTTPException(status_code=400, detail="Invalid WhatsApp status")
                        correlation_ids.append(_enqueue_or_retry(
                            kind="status",
                            payload=dict(status),
                            phone_number_id=phone_number_id,
                            client_id=current_client_id,
                        ))

        from app.services.whatsapp_observability import process_metrics

        process_metrics.observe_webhook_ack(
            (perf_counter() - started_at) * 1000, client_ids=observed_client_ids
        )
        if correlation_ids and isinstance(correlation_ids[0], str):
            response.headers["X-Correlation-ID"] = correlation_ids[0]
        return {"status": "queued"}
    from app.services.whatsapp_observability import process_metrics

    process_metrics.observe_webhook_ack((perf_counter() - started_at) * 1000)
    return {"status": "ignored"}


def _enqueue_or_retry(*, kind: str, payload: dict, phone_number_id: str, client_id: int) -> str:
    """Never substitute in-process work when the durable queue is unavailable."""
    from app.services.whatsapp_queue import PermanentWebhookError, enqueue_event

    try:
        return enqueue_event(
            kind=kind,
            payload=payload,
            phone_number_id=phone_number_id,
            client_id=client_id,
        )
    except PermanentWebhookError as exc:
        raise HTTPException(status_code=400, detail="Invalid WhatsApp event") from exc
    except Exception as exc:
        logger.error("WhatsApp durable enqueue failed: %s", type(exc).__name__)
        # Meta retries non-2xx delivery. A 503 is safer than acknowledging an
        # event that is not durable in Redis/RQ.
        raise HTTPException(status_code=503, detail="Webhook queue unavailable") from exc

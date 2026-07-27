from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.api.runtime import logger
from app.core.database import SessionLocal
from app.core.models import Client, EmailSuppression, Lead, Message
from app.email.email_ai import generate_email_draft
from app.email.email_client import EmailSendError, email_client
from app.email.email_templates import (
    apply_merge_fields,
    build_unsubscribe_url,
    is_valid_email_format,
    verify_unsub_token,
    wrap_email_bodies,
)
from app.email.email_validation import validate_lead_email
from app.email.email_webhooks import (
    handle_resend_event,
    parse_event_json,
    verify_resend_signature,
)
from app.services.usage import check_limit, log_usage

router = APIRouter()

_BLOCKED_EMAIL_STATUSES = frozenset({"bounced", "complained", "unsubscribed"})
_MAX_EMAIL_SUBJECT_LEN = 500
_MAX_EMAIL_BODY_LEN = 100_000

class EmailSettingsUpdateBody(BaseModel):
    email_enabled: bool | None = None
    email_from_address: str | None = None
    email_from_name: str | None = None
    email_reply_to: str | None = None
    email_company_address: str | None = None
    email_footer_html: str | None = None


class EmailSendBody(BaseModel):
    lead_id: int
    subject: str
    body_text: str
    body_html: str | None = None


class EmailDraftBody(BaseModel):
    lead_id: int
    intent: str | None = "initial outreach"
    notes: str | None = None
    use_rag: bool = True
    # Only honored when EMAIL_AI_AUTO_SEND=true and draft confidence passes.
    send: bool = False


def _email_settings_payload(db_client: Client) -> dict:
    platform = email_client.status()
    return {
        "email_enabled": bool(db_client.email_enabled),
        "email_provider": db_client.email_provider or "resend",
        "email_from_address": db_client.email_from_address or "",
        "email_from_name": db_client.email_from_name or "",
        "email_reply_to": db_client.email_reply_to or "",
        "email_company_address": db_client.email_company_address or "",
        "email_footer_html": db_client.email_footer_html or "",
        "platform": platform,
    }


@router.get("/api/settings/email")
@limiter.limit("120/minute", key_func=get_client_key)
def get_email_settings(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    """Return tenant email channel settings + platform readiness (no secrets)."""
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")
        return _email_settings_payload(db_client)


@router.patch("/api/settings/email")
@limiter.limit("60/minute", key_func=get_client_key)
def update_email_settings(
    request: Request,
    response: Response,
    body: EmailSettingsUpdateBody,
    client: Client = Depends(require_api_key),
):
    """Partial update of tenant email settings. Does not store API keys (platform env)."""
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    if body.email_from_address is not None and body.email_from_address.strip():
        if not is_valid_email_format(body.email_from_address):
            raise HTTPException(status_code=400, detail="email_from_address is not a valid email")
    if body.email_reply_to is not None and body.email_reply_to.strip():
        if not is_valid_email_format(body.email_reply_to):
            raise HTTPException(status_code=400, detail="email_reply_to is not a valid email")

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        if body.email_enabled is not None:
            db_client.email_enabled = body.email_enabled
        if body.email_from_address is not None:
            db_client.email_from_address = body.email_from_address.strip() or None
        if body.email_from_name is not None:
            db_client.email_from_name = body.email_from_name.strip() or None
        if body.email_reply_to is not None:
            db_client.email_reply_to = body.email_reply_to.strip() or None
        if body.email_company_address is not None:
            db_client.email_company_address = body.email_company_address.strip() or None
        if body.email_footer_html is not None:
            db_client.email_footer_html = body.email_footer_html or None

        s.commit()
        s.refresh(db_client)
        return {"success": True, **_email_settings_payload(db_client)}


@router.post("/api/email/send")
@limiter.limit("30/minute", key_func=get_client_key)
def send_email_to_lead(
    request: Request,
    response: Response,
    body: EmailSendBody,
    client: Client = Depends(require_api_key),
):
    """
    Send one outbound email to a lead (Phase E2).

    Requires: platform email configured, tenant email_enabled + from address,
    lead.email present and not suppressed / bounced, monthly plan cap.
    Always injects unsubscribe link + company address footer when available.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    subject = (body.subject or "").strip()
    body_text = body.body_text or ""
    body_html = body.body_html

    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    if len(subject) > _MAX_EMAIL_SUBJECT_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"subject exceeds {_MAX_EMAIL_SUBJECT_LEN} characters",
        )
    if not body_text.strip() and not (body_html or "").strip():
        raise HTTPException(status_code=400, detail="body_text or body_html is required")
    if len(body_text) > _MAX_EMAIL_BODY_LEN or len(body_html or "") > _MAX_EMAIL_BODY_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"body exceeds {_MAX_EMAIL_BODY_LEN} characters",
        )

    if not email_client.is_ready():
        raise HTTPException(
            status_code=503,
            detail=(
                "Email platform is not configured. Set EMAIL_PLATFORM_ENABLED=true "
                "and RESEND_API_KEY on the server."
            ),
        )

    plan = client.plan_tier or "base"
    allowed, reason = check_limit(client.id, "email_sent", plan=plan)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason or "Email plan limit reached")

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        if not db_client.email_enabled:
            raise HTTPException(
                status_code=400,
                detail="Email is disabled for this tenant. Enable it via PATCH /api/settings/email.",
            )

        from_address = (db_client.email_from_address or "").strip() or (
            email_client.default_from_address or ""
        ).strip()
        if not from_address:
            raise HTTPException(
                status_code=400,
                detail="email_from_address is not set for this tenant (and no platform default).",
            )

        lead = (
            s.query(Lead)
            .filter(Lead.id == body.lead_id, Lead.client_id == client.id)
            .first()
        )
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        email_check = validate_lead_email(lead.email, allow_empty=False, block_disposable=True)
        if not email_check.ok or not email_check.email:
            raise HTTPException(
                status_code=400,
                detail=email_check.error or "Lead has no valid email address on file",
            )
        to_email = email_check.email

        status_norm = (lead.email_status or "").strip().lower()
        if status_norm in _BLOCKED_EMAIL_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot send: lead email_status is '{status_norm}'",
            )

        suppressed = (
            s.query(EmailSuppression)
            .filter(
                EmailSuppression.client_id == client.id,
                EmailSuppression.email == to_email,
            )
            .first()
        )
        if suppressed:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot send: email is suppressed ({suppressed.reason})",
            )

        try:
            unsub_url = build_unsubscribe_url(client.id, to_email)
        except ValueError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        merge = {
            "name": lead.name or "",
            "business_name": lead.business_name or "",
            "calendly_link": db_client.calendly_link or "",
            "email": to_email,
            "company_display_name": db_client.company_display_name or db_client.name or "",
        }
        subject_final = apply_merge_fields(subject, merge)
        text_merged = apply_merge_fields(body_text, merge)
        html_merged = apply_merge_fields(body_html, merge) if body_html else None
        final_text, final_html = wrap_email_bodies(
            body_text=text_merged,
            body_html=html_merged,
            company_address=db_client.email_company_address,
            unsubscribe_url=unsub_url,
            custom_footer_html=db_client.email_footer_html,
        )

        headers = {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

        try:
            result = email_client.send_email(
                to=to_email,
                subject=subject_final,
                text=final_text,
                html=final_html,
                from_address=from_address,
                from_name=db_client.email_from_name or email_client.default_from_name or None,
                reply_to=(db_client.email_reply_to or None),
                headers=headers,
                tags={
                    "client_id": str(client.id),
                    "lead_id": str(lead.id),
                },
            )
        except EmailSendError as e:
            logger.error("Email send failed for lead %s: %s", lead.id, e)
            # Map provider rejections to 502; config/cap to 503/429
            msg = str(e)
            if "Daily email send cap" in msg:
                raise HTTPException(status_code=429, detail=msg) from e
            if "not configured" in msg.lower() or "No from_address" in msg:
                raise HTTPException(status_code=503, detail=msg) from e
            raise HTTPException(
                status_code=502,
                detail=msg if not e.body else f"{msg}: {e.body}",
            ) from e

        msg_row = Message(
            lead_id=lead.id,
            direction="OUTBOUND",
            msg_type="email",
            body=final_text,
            channel="email",
            subject=subject_final,
            provider_message_id=result.provider_message_id,
            status="sent",
            email_headers={"List-Unsubscribe": unsub_url},
            provider_metadata={"raw_id": result.provider_message_id},
        )
        s.add(msg_row)

        # First-touch stage bump (mirrors WhatsApp inbound / outreach pattern).
        if (lead.status or "") == "New Lead":
            lead.status = "Contacted"
            lead.updated_at = datetime.utcnow()

        if not lead.email_status:
            lead.email_status = "valid"

        s.commit()
        s.refresh(msg_row)
        message_id = msg_row.id

    log_usage(client.id, "email_sent", tokens_used=0, cost_estimate=0.0)

    return {
        "success": True,
        "lead_id": body.lead_id,
        "to": to_email,
        "subject": subject_final,
        "provider_message_id": result.provider_message_id,
        "message_id": message_id,
    }


@router.post("/api/email/draft")
@limiter.limit("20/minute", key_func=get_client_key)
def draft_email_for_lead(
    request: Request,
    response: Response,
    body: EmailDraftBody,
    client: Client = Depends(require_api_key),
):
    """
    AI-personalized email draft for a lead (Phase E5).

    Default: returns subject + body_text for human review (does not send).
    Optional send=true only works when EMAIL_AI_AUTO_SEND=true AND confidence OK.
    """
    from app.core.config import EMAIL_AI_AUTO_SEND
    from app.services.guardrails import CONFIDENCE_THRESHOLD
    from app.services.rag import retrieve_context
    from app.services import tenant as tenant_mod

    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    plan = client.plan_tier or "base"
    # Drafts consume AI capacity
    allowed, reason = check_limit(client.id, "ai_response", plan=plan)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason or "AI plan limit reached")

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        lead = (
            s.query(Lead)
            .filter(Lead.id == body.lead_id, Lead.client_id == client.id)
            .first()
        )
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        conversation = lead.last_message or ""
        lead_name = lead.name or "there"
        business_name = lead.business_name
        lead_email = lead.email
        company = db_client.company_display_name or db_client.name
        calendly = db_client.calendly_link
        lead_id = lead.id
        # Build Gemini while session is open (tenant reads system_prompt fields)
        gemini = tenant_mod.get_gemini_for_client(db_client)

    rag_context = None
    rag_chunks = 0
    if body.use_rag:
        try:
            query = (body.intent or "outreach") + " " + (body.notes or "") + " " + lead_name
            chunks = retrieve_context(client.id, query.strip(), top_k=3)
            if chunks:
                rag_chunks = len(chunks)
                rag_context = "\n---\n".join(chunks)
        except Exception as e:
            logger.warning("RAG for email draft failed (continuing without): %s", e)
    draft = generate_email_draft(
        gemini,
        lead_name=lead_name,
        business_name=business_name,
        lead_email=lead_email,
        company_display_name=company,
        calendly_link=calendly,
        intent=body.intent or "initial outreach",
        notes=body.notes,
        conversation_excerpt=conversation,
        rag_context=rag_context,
        confidence_threshold=CONFIDENCE_THRESHOLD,
    )

    if draft.tokens_estimate:
        log_usage(client.id, "email_ai_draft", draft.tokens_estimate, 0.0)
        # Also count toward ai_response monthly cap
        log_usage(client.id, "ai_response", max(1, draft.tokens_estimate // 4), 0.0)

    payload = {
        "success": draft.ok,
        "lead_id": lead_id,
        "subject": draft.subject,
        "body_text": draft.body_text,
        "confidence": draft.confidence,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "rag_chunks_used": rag_chunks or draft.rag_chunks_used,
        "auto_send_enabled": EMAIL_AI_AUTO_SEND,
        "sent": False,
        "error": draft.error,
    }

    if not draft.ok:
        # Still return partial draft for human edit when model produced text
        if draft.subject or draft.body_text:
            payload["review_required"] = True
            return payload
        raise HTTPException(status_code=422, detail=draft.error or "Draft generation failed")

    # Optional auto-send (explicitly opt-in via env + request flag)
    if body.send:
        if not EMAIL_AI_AUTO_SEND:
            payload["error"] = (
                "Auto-send is disabled. Set EMAIL_AI_AUTO_SEND=true to allow "
                "send=true, or call POST /api/email/send with this draft."
            )
            payload["review_required"] = True
            return payload

        # Reuse send endpoint logic by constructing internal call shape
        send_body = EmailSendBody(
            lead_id=lead_id,
            subject=draft.subject or "",
            body_text=draft.body_text or "",
            body_html=None,
        )
        try:
            send_result = send_email_to_lead(request, response, send_body, client)
            payload["sent"] = True
            payload["send_result"] = send_result
        except HTTPException as e:
            payload["sent"] = False
            payload["error"] = e.detail
            payload["review_required"] = True
            return payload

    return payload


@router.post("/api/webhooks/email/resend")
@limiter.limit("100/minute")
async def resend_email_webhook(request: Request, response: Response):
    """
    Resend delivery events (Phase E3).

    Signature verified first (Svix headers + RESEND_WEBHOOK_SECRET), same
    fail-closed pattern as WhatsApp HMAC and Razorpay webhooks.
    """
    from app.core.config import RESEND_WEBHOOK_SECRET

    body_bytes = await request.body()
    if not RESEND_WEBHOOK_SECRET:
        logger.error("RESEND_WEBHOOK_SECRET not set — rejecting email webhook")
        raise HTTPException(status_code=503, detail="Email webhook secret not configured")

    svix_id = request.headers.get("svix-id")
    svix_timestamp = request.headers.get("svix-timestamp")
    svix_signature = request.headers.get("svix-signature")

    if not verify_resend_signature(
        body_bytes,
        svix_id=svix_id,
        svix_timestamp=svix_timestamp,
        svix_signature=svix_signature,
    ):
        logger.warning("Invalid Resend webhook signature rejected")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        event = parse_event_json(body_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    result = handle_resend_event(event)
    return {"status": "ok", "result": result}


def _unsubscribe_html(title: str, message: str, *, ok: bool = True) -> str:
    color = "#0a7" if ok else "#a30"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.25rem; color: {color}; }}
  p {{ line-height: 1.5; color: #444; }}
</style></head>
<body>
  <h1>{title}</h1>
  <p>{message}</p>
</body></html>"""


@router.get("/api/email/unsubscribe", response_class=HTMLResponse)
@limiter.limit("30/minute")
def email_unsubscribe(request: Request, response: Response, token: str = ""):
    """
    Public one-click unsubscribe (Phase E3).

    Token is HMAC-signed (email_templates.make_unsub_token). On success:
    insert email_suppressions + set lead.email_status = unsubscribed.
    """
    if not token:
        return HTMLResponse(
            content=_unsubscribe_html(
                "Invalid link",
                "This unsubscribe link is missing a token. You can close this page.",
                ok=False,
            ),
            status_code=400,
        )

    try:
        parsed = verify_unsub_token(token)
    except ValueError:
        # Signing secret not configured on server
        return HTMLResponse(
            content=_unsubscribe_html(
                "Unavailable",
                "Unsubscribe is temporarily unavailable. Please try again later.",
                ok=False,
            ),
            status_code=503,
        )

    if not parsed:
        return HTMLResponse(
            content=_unsubscribe_html(
                "Invalid or expired link",
                "This unsubscribe link is invalid or has expired. "
                "If you still receive email, reply STOP or contact the sender.",
                ok=False,
            ),
            status_code=400,
        )

    client_id, email = parsed
    if not SessionLocal:
        return HTMLResponse(
            content=_unsubscribe_html(
                "Unavailable",
                "We could not process your request right now. Please try again later.",
                ok=False,
            ),
            status_code=503,
        )

    from sqlalchemy import func as sa_func

    with SessionLocal() as s:
        existing = (
            s.query(EmailSuppression)
            .filter(
                EmailSuppression.client_id == client_id,
                EmailSuppression.email == email,
            )
            .first()
        )
        if not existing:
            s.add(
                EmailSuppression(
                    client_id=client_id,
                    email=email,
                    reason="unsubscribed",
                    created_at=datetime.utcnow(),
                )
            )
        else:
            existing.reason = "unsubscribed"

        leads = (
            s.query(Lead)
            .filter(
                Lead.client_id == client_id,
                sa_func.lower(Lead.email) == email,
            )
            .all()
        )
        for lead in leads:
            lead.email_status = "unsubscribed"
            lead.updated_at = datetime.utcnow()

        s.commit()

    logger.info("Email unsubscribed: client_id=%s email=%s", client_id, email)
    return HTMLResponse(
        content=_unsubscribe_html(
            "You are unsubscribed",
            "You will no longer receive marketing or outreach emails from this sender "
            f"at <strong>{email}</strong>. You can close this page.",
            ok=True,
        ),
        status_code=200,
    )


@router.post("/api/email/unsubscribe", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def email_unsubscribe_post(request: Request, response: Response, token: str = ""):
    """
    One-click List-Unsubscribe=One-Click POST support (RFC 8058).

    Some clients POST instead of GET; reuse the same token handling.
    """
    # Prefer query token; also accept form field if present
    if not token:
        try:
            form = await request.form()
            token = str(form.get("token") or "")
        except Exception:
            token = ""
    # Delegate to GET handler logic
    return email_unsubscribe(request, response, token=token)


# ── Email campaigns / sequences (Phase E7) ────────────────────────────────

"""
Inbound email processing — Phase E6.

Flow:
  email.received webhook (metadata only)
    → fetch body via Resend Receiving API
    → strip quoted reply / signature noise
    → match lead (by from-email + tenant from to-address)
    → store INBOUND message
    → human takeover / limits / guardrails
    → AI email reply (optional, EMAIL_AI_AUTO_REPLY)
    → send + store OUTBOUND
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr
from html import unescape
from typing import Any

from sqlalchemy import func

from app.core.config import (
    EMAIL_AI_AUTO_REPLY,
    EMAIL_DEFAULT_FROM_ADDRESS,
)
from app.core.database import SessionLocal, is_configured
from app.email.email_client import EmailClient, EmailSendError, email_client
from app.email.email_templates import build_unsubscribe_url, wrap_email_bodies
from app.services.guardrails import (
    CONFIDENCE_THRESHOLD,
    redact_pii,
    scan_input,
    score_confidence,
)
from app.core.models import Client, EmailSuppression, Lead, Message
from app.services.usage import check_limit, estimate_tokens, log_usage

logger = logging.getLogger(__name__)

# ── Quote / signature stripping ───────────────────────────────────────────

_QUOTE_MARKERS = [
    re.compile(r"^On .+ wrote:\s*$", re.I | re.M),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.I | re.M),
    re.compile(r"^From:\s+.+$", re.I | re.M),
    re.compile(r"^_{5,}\s*$", re.M),
    re.compile(r"^>+", re.M),  # used line-by-line below
]

_SIG_MARKERS = [
    re.compile(r"^--\s*$", re.M),
    re.compile(r"^Sent from my (iPhone|iPad|Android|Galaxy|Mobile)", re.I | re.M),
    re.compile(r"^Get Outlook for", re.I | re.M),
]


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def strip_quoted_reply(text: str) -> str:
    """
    Best-effort extraction of the new reply text from a plain-text email body.
    Removes common quote headers, quoted lines, and mobile signatures.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cut_at = len(lines)

    for i, line in enumerate(lines):
        stripped = line.strip()
        # Classic "On <date> <person> wrote:"
        if re.match(r"^On .+ wrote:\s*$", stripped, re.I):
            cut_at = i
            break
        if re.match(r"^-{2,}\s*Original Message\s*-{2,}\s*$", stripped, re.I):
            cut_at = i
            break
        if re.match(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", stripped, re.I):
            cut_at = i
            break
        if stripped == "--" or stripped == "-- ":
            cut_at = i
            break
        if re.match(r"^Sent from my (iPhone|iPad|Android|Galaxy|Mobile)", stripped, re.I):
            cut_at = i
            break
        if re.match(r"^Get Outlook for", stripped, re.I):
            cut_at = i
            break
        # Gmail-style header block
        if re.match(r"^From:\s+\S+", stripped, re.I) and i > 0:
            # only if following looks like headers
            if i + 1 < len(lines) and re.match(
                r"^(Sent|To|Subject|Date):", lines[i + 1].strip(), re.I
            ):
                cut_at = i
                break

    kept = lines[:cut_at]
    # Drop leading quote markers on remaining lines
    cleaned: list[str] = []
    for line in kept:
        if re.match(r"^>+", line.strip()):
            continue
        cleaned.append(line)

    result = "\n".join(cleaned).strip()
    # If stripping wiped everything, fall back to original (short) body
    if not result and text.strip():
        return text.strip()[:2000]
    return result


def extract_body_text(received: dict[str, Any]) -> str:
    text = (received.get("text") or "").strip()
    if text:
        return text
    html = received.get("html") or ""
    return _html_to_text(html)


def normalize_from_address(from_field: str | None) -> str:
    """'Name <a@b.com>' → 'a@b.com'"""
    if not from_field:
        return ""
    _name, addr = parseaddr(from_field)
    return (addr or from_field).strip().lower()


def _reply_subject(subject: str | None) -> str:
    s = (subject or "").strip() or "(no subject)"
    if re.match(r"^(re|fw|fwd)\s*:", s, re.I):
        return s
    return f"Re: {s}"


@dataclass
class InboundProcessResult:
    code: str
    lead_id: int | None = None
    client_id: int | None = None
    inbound_message_id: int | None = None
    outbound_provider_id: str | None = None
    detail: str | None = None


def _resolve_lead_and_client(
    session,
    *,
    from_email: str,
    to_addresses: list[str],
) -> tuple[Lead | None, Client | None, str]:
    """
    Match inbound mail to a tenant lead.

    Prefer: lead.email == from AND client's from/reply-to in to_addresses.
    Fallback: unique lead with that email across tenants.
    """
    if not from_email:
        return None, None, "no_from"

    to_set = {a.strip().lower() for a in to_addresses if a}

    candidates = (
        session.query(Lead)
        .filter(func.lower(Lead.email) == from_email)
        .all()
    )
    if not candidates:
        return None, None, "lead_not_found"

    if len(candidates) == 1:
        lead = candidates[0]
        client = session.get(Client, lead.client_id)
        return lead, client, "matched_unique"

    # Disambiguate by recipient matching tenant from/reply addresses
    matched: list[tuple[Lead, Client]] = []
    for lead in candidates:
        client = session.get(Client, lead.client_id)
        if not client:
            continue
        tenant_addrs = {
            (client.email_from_address or "").strip().lower(),
            (client.email_reply_to or "").strip().lower(),
            (EMAIL_DEFAULT_FROM_ADDRESS or "").strip().lower(),
        }
        tenant_addrs.discard("")
        if tenant_addrs & to_set:
            matched.append((lead, client))

    if len(matched) == 1:
        return matched[0][0], matched[0][1], "matched_by_to"
    if len(matched) > 1:
        return None, None, "ambiguous_tenant"
    # Multiple leads, no to-match — refuse rather than leak
    return None, None, "ambiguous_lead"


def _build_email_history_for_ai(session, lead_id: int, limit: int = 20) -> str:
    """Flatten recent email-channel messages for the LLM."""
    rows = (
        session.query(Message)
        .filter(Message.lead_id == lead_id, Message.channel == "email")
        .order_by(Message.created_at.desc())
        .limit(limit)
        .all()
    )
    rows = list(reversed(rows))
    lines: list[str] = []
    for m in rows:
        role = "THEM" if (m.direction or "").upper() == "INBOUND" else "US"
        subj = f" [{m.subject}]" if m.subject else ""
        body = (m.body or "")[:1500]
        lines.append(f"{role}{subj}: {body}")
    return "\n".join(lines)


def process_inbound_email_event(
    data: dict[str, Any],
    *,
    client: EmailClient | None = None,
    auto_reply: bool | None = None,
) -> InboundProcessResult:
    """
    Handle a verified `email.received` data payload.

    auto_reply: override EMAIL_AI_AUTO_REPLY when not None.
    """
    if not is_configured() or not SessionLocal:
        return InboundProcessResult(code="db_not_configured")

    ec = client or email_client
    do_auto = EMAIL_AI_AUTO_REPLY if auto_reply is None else auto_reply

    email_id = (data.get("email_id") or data.get("id") or "").strip()
    if not email_id:
        return InboundProcessResult(code="missing_email_id")

    from_raw = data.get("from") or ""
    from_email = normalize_from_address(from_raw if isinstance(from_raw, str) else str(from_raw))
    to_list = data.get("to") or []
    if isinstance(to_list, str):
        to_list = [to_list]
    to_list = [normalize_from_address(t) if isinstance(t, str) else str(t) for t in to_list]
    # Also consider received_for
    for extra in data.get("received_for") or []:
        to_list.append(normalize_from_address(extra if isinstance(extra, str) else str(extra)))

    meta_subject = (data.get("subject") or "").strip()
    meta_message_id = (data.get("message_id") or "").strip() or None

    # Fetch full body
    try:
        received = ec.fetch_received_email(email_id)
    except EmailSendError as e:
        logger.error("Inbound fetch failed for %s: %s", email_id, e)
        return InboundProcessResult(code="fetch_failed", detail=str(e))

    raw_body = extract_body_text(received)
    cleaned = strip_quoted_reply(raw_body)
    subject = (received.get("subject") or meta_subject or "").strip()
    headers = received.get("headers") if isinstance(received.get("headers"), dict) else {}
    message_id_hdr = (
        headers.get("message-id")
        or headers.get("Message-Id")
        or headers.get("Message-ID")
        or meta_message_id
    )
    in_reply_to = headers.get("in-reply-to") or headers.get("In-Reply-To")

    with SessionLocal() as session:
        # Idempotency
        existing = (
            session.query(Message)
            .filter(
                Message.provider_message_id == email_id,
                Message.channel == "email",
                Message.direction == "INBOUND",
            )
            .first()
        )
        if existing:
            return InboundProcessResult(
                code="duplicate",
                lead_id=existing.lead_id,
                inbound_message_id=existing.id,
            )

        lead, db_client, match_code = _resolve_lead_and_client(
            session, from_email=from_email, to_addresses=to_list
        )
        if not lead or not db_client:
            logger.info(
                "Inbound email unmatched from=%s to=%s reason=%s",
                from_email,
                to_list,
                match_code,
            )
            return InboundProcessResult(code=match_code, detail=from_email)

        # Thread id: prefer In-Reply-To chain, else inbound message-id, else email_id
        thread_id = (in_reply_to or message_id_hdr or email_id or "").strip() or None

        inbound_msg = Message(
            lead_id=lead.id,
            direction="INBOUND",
            msg_type="email",
            body=cleaned or raw_body or "(empty body)",
            channel="email",
            subject=subject or None,
            provider_message_id=email_id,
            thread_id=thread_id,
            status="received",
            email_headers={
                "from": from_email,
                "to": to_list,
                "message_id": message_id_hdr,
                "in_reply_to": in_reply_to,
            },
            provider_metadata={
                "raw_preview": (raw_body or "")[:500],
                "match": match_code,
            },
            created_at=datetime.utcnow(),
        )
        session.add(inbound_msg)
        lead.updated_at = datetime.utcnow()
        if (lead.status or "") == "New Lead":
            lead.status = "Contacted"
        session.commit()
        session.refresh(inbound_msg)

        result = InboundProcessResult(
            code="stored",
            lead_id=lead.id,
            client_id=db_client.id,
            inbound_message_id=inbound_msg.id,
        )

        # Human takeover — store only
        if lead.is_human_takeover:
            logger.info(
                "Inbound email stored; human takeover active for lead %s",
                lead.id,
            )
            result.code = "stored_takeover"
            return result

        if not do_auto:
            result.code = "stored_no_auto_reply"
            return result

        if not db_client.email_enabled:
            result.code = "stored_email_disabled"
            return result

        # Suppression check on our outbound path (don't email suppressed)
        if from_email:
            sup = (
                session.query(EmailSuppression)
                .filter(
                    EmailSuppression.client_id == db_client.id,
                    EmailSuppression.email == from_email,
                )
                .first()
            )
            if sup:
                result.code = "stored_suppressed"
                return result

        plan = db_client.plan_tier or "base"
        allowed, reason = check_limit(db_client.id, "ai_response", plan=plan)
        if not allowed:
            logger.warning("Inbound AI reply blocked by limit: %s", reason)
            lead.is_human_takeover = True
            session.commit()
            result.code = "stored_limit_takeover"
            result.detail = reason
            return result

        # Guardrails on cleaned inbound text
        user_text = cleaned or raw_body or ""
        is_safe, refusal = scan_input(user_text)
        if not is_safe:
            # Do not call LLM; optional: no auto-send of refusal email for injection
            logger.warning("Inbound email injection blocked for lead %s", lead.id)
            result.code = "stored_injection_blocked"
            return result

        llm_text = redact_pii(user_text)

        # RAG
        rag_context = ""
        try:
            from rag import retrieve_context

            chunks = retrieve_context(db_client.id, llm_text, top_k=3)
            if chunks:
                rag_context = "\n---\n".join(chunks)
        except Exception as e:
            logger.warning("RAG for inbound email failed: %s", e)

        import tenant as tenant_mod

        gemini = tenant_mod.get_gemini_for_client(db_client)
        history_blob = _build_email_history_for_ai(session, lead.id)

        email_rules = """
EMAIL REPLY RULES:
- Reply as a professional email (not WhatsApp chat).
- Output JSON only: {"subject": "...", "body_text": "..."}.
- Subject should usually start with Re: when replying.
- Do not invent facts. Keep 2–6 short paragraphs.
- Do not include unsubscribe text (system adds it).
"""
        original_prompt = getattr(gemini, "_system_prompt", "") or ""
        gemini._system_prompt = original_prompt.rstrip() + "\n" + email_rules
        if rag_context:
            gemini._system_prompt += (
                "\n\nKNOWLEDGE BASE:\n" + rag_context + "\n"
            )

        user_prompt = (
            f"Lead: {lead.name or from_email}\n"
            f"Business: {lead.business_name or 'N/A'}\n"
            f"Their email: {from_email}\n"
            f"Our company: {db_client.company_display_name or db_client.name}\n"
            f"Booking link: {db_client.calendly_link or 'N/A'}\n"
            f"Inbound subject: {subject}\n\n"
            f"Email thread so far:\n{history_blob}\n\n"
            f"Latest inbound message to answer:\n{llm_text}\n\n"
            'Return JSON: {"subject": "...", "body_text": "..."}'
        )

        try:
            raw_ai = gemini.generate_response_with_history([], user_prompt)
        except Exception as e:
            logger.error("Inbound AI reply generation failed: %s", e)
            gemini._system_prompt = original_prompt
            result.code = "stored_ai_failed"
            result.detail = str(e)
            return result
        finally:
            gemini._system_prompt = original_prompt

        from email_ai import _parse_subject_body

        reply_subject, reply_body = _parse_subject_body(str(raw_ai or ""))
        if not reply_body:
            result.code = "stored_ai_unparseable"
            return result
        if not reply_subject:
            reply_subject = _reply_subject(subject)

        conf = score_confidence(
            f"{reply_subject}\n{reply_body}",
            original_prompt + "\n" + (db_client.calendly_link or ""),
        )
        if conf < CONFIDENCE_THRESHOLD:
            logger.warning(
                "Low confidence email reply (%.2f) for lead %s — takeover",
                conf,
                lead.id,
            )
            lead.is_human_takeover = True
            session.commit()
            result.code = "stored_low_confidence"
            result.detail = f"confidence={conf:.2f}"
            return result

        # Send
        from_address = (db_client.email_from_address or EMAIL_DEFAULT_FROM_ADDRESS or "").strip()
        if not from_address:
            result.code = "stored_no_from"
            return result

        try:
            unsub_url = build_unsubscribe_url(db_client.id, from_email)
        except ValueError as e:
            result.code = "stored_unsub_config"
            result.detail = str(e)
            return result

        final_text, final_html = wrap_email_bodies(
            body_text=reply_body,
            body_html=None,
            company_address=db_client.email_company_address,
            unsubscribe_url=unsub_url,
            custom_footer_html=db_client.email_footer_html,
        )

        send_headers: dict[str, str] = {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        if message_id_hdr:
            send_headers["In-Reply-To"] = message_id_hdr
            send_headers["References"] = message_id_hdr

        try:
            send_result = ec.send_email(
                to=from_email,
                subject=reply_subject,
                text=final_text,
                html=final_html,
                from_address=from_address,
                from_name=db_client.email_from_name or None,
                reply_to=db_client.email_reply_to or None,
                headers=send_headers,
                tags={
                    "client_id": str(db_client.id),
                    "lead_id": str(lead.id),
                    "kind": "inbound_reply",
                },
            )
        except EmailSendError as e:
            logger.error("Inbound AI reply send failed: %s", e)
            result.code = "stored_send_failed"
            result.detail = str(e)
            return result

        out_msg = Message(
            lead_id=lead.id,
            direction="OUTBOUND",
            msg_type="email",
            body=final_text,
            channel="email",
            subject=reply_subject,
            provider_message_id=send_result.provider_message_id,
            thread_id=thread_id,
            status="sent",
            email_headers=send_headers,
            provider_metadata={"kind": "inbound_ai_reply", "confidence": conf},
            created_at=datetime.utcnow(),
        )
        session.add(out_msg)
        session.commit()

        tokens = estimate_tokens(user_prompt) + estimate_tokens(reply_body)
        log_usage(db_client.id, "email_ai_response", tokens, 0.0)
        log_usage(db_client.id, "ai_response", max(1, tokens // 4), 0.0)
        log_usage(db_client.id, "email_sent", 0, 0.0)

        result.code = "replied"
        result.outbound_provider_id = send_result.provider_message_id
        logger.info(
            "Inbound email AI reply sent lead=%s conf=%.2f",
            lead.id,
            conf,
        )
        return result

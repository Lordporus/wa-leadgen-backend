"""
AI email personalization — Phase E5.

Generates subject + body for a lead using the tenant's Gemini client,
email channel adaptation (professional, not WhatsApp Hinglish), optional RAG,
guardrails (input scan, PII redact, confidence score).

Default is draft-only. Auto-send is gated by config.EMAIL_AI_AUTO_SEND.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.services.guardrails import (
    CONFIDENCE_THRESHOLD,
    redact_pii,
    scan_input,
    score_confidence,
)
from app.services.usage import estimate_tokens

logger = logging.getLogger(__name__)

_EMAIL_CHANNEL_ADAPTATION = """
EMAIL CHANNEL RULES (override any WhatsApp-style instructions for this task only):
- Write a professional email, not a WhatsApp chat message.
- Use clear English (or match the lead's language if conversation history is clearly Hindi/Hinglish).
- Structure: short greeting, 2–5 sentence body, clear CTA, sign-off using the company name.
- Subject line: max ~70 characters, specific, no spammy ALL CAPS or excessive punctuation.
- Do NOT invent pricing, medical claims, or credentials not present in the context.
- Do NOT include unsubscribe text (the system appends that automatically).
- Do NOT use unresolved placeholders like [NAME], {{name}}, or TODO.
- Prefer the calendly/booking link from context when asking for a meeting.
- Output MUST be valid JSON only (no markdown fences) with keys:
  "subject" (string) and "body_text" (string, plain text with newlines OK).
"""


@dataclass
class EmailDraftResult:
    ok: bool
    subject: str | None = None
    body_text: str | None = None
    confidence: float | None = None
    error: str | None = None
    raw_model_output: str | None = None
    tokens_estimate: int = 0
    rag_chunks_used: int = 0


def _strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _parse_subject_body(raw: str) -> tuple[str | None, str | None]:
    cleaned = _strip_code_fences(raw)
    # Primary: JSON object
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            subject = (data.get("subject") or data.get("Subject") or "").strip()
            body = (
                data.get("body_text")
                or data.get("body")
                or data.get("Body")
                or data.get("text")
                or ""
            )
            body = str(body).strip()
            if subject or body:
                return subject or None, body or None
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: look for first JSON object substring
    m = re.search(r"\{[\s\S]*\}", cleaned)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                subject = (data.get("subject") or "").strip()
                body = (data.get("body_text") or data.get("body") or "").strip()
                if subject or body:
                    return subject or None, body or None
        except (json.JSONDecodeError, TypeError):
            pass

    # Last resort: "Subject: ..." first line
    lines = cleaned.splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
        return subject or None, body or None

    return None, None


def build_email_user_prompt(
    *,
    lead_name: str,
    business_name: str | None,
    lead_email: str | None,
    company_display_name: str | None,
    calendly_link: str | None,
    intent: str,
    notes: str | None,
    conversation_excerpt: str | None,
    rag_context: str | None,
) -> str:
    parts = [
        "Draft one personalized outreach email for the following lead.",
        f"Intent: {intent}",
        f"Lead name: {lead_name or 'there'}",
        f"Business name: {business_name or 'N/A'}",
        f"Lead email: {lead_email or 'N/A'}",
        f"Our company: {company_display_name or 'our team'}",
        f"Booking link: {calendly_link or 'N/A'}",
    ]
    if notes and notes.strip():
        parts.append(f"Extra instructions from the agent: {notes.strip()}")
    if conversation_excerpt and conversation_excerpt.strip():
        # Cap history so prompts stay bounded
        excerpt = conversation_excerpt.strip()
        if len(excerpt) > 4000:
            excerpt = excerpt[-4000:]
        parts.append("Recent conversation history (may be WhatsApp):\n" + excerpt)
    if rag_context and rag_context.strip():
        parts.append("Knowledge base context:\n" + rag_context.strip())
    parts.append(
        'Return JSON only: {"subject": "...", "body_text": "..."}'
    )
    return "\n\n".join(parts)


def generate_email_draft(
    gemini_client: Any,
    *,
    lead_name: str,
    business_name: str | None = None,
    lead_email: str | None = None,
    company_display_name: str | None = None,
    calendly_link: str | None = None,
    intent: str = "initial outreach",
    notes: str | None = None,
    conversation_excerpt: str | None = None,
    rag_context: str | None = None,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
) -> EmailDraftResult:
    """
    Run guardrails + LLM and return a draft (never sends).
    """
    intent_safe = (intent or "initial outreach").strip()[:500]
    notes_safe = (notes or "").strip()[:2000]

    # Layer 1: scan free-form agent notes / intent for injection
    for label, text in (("intent", intent_safe), ("notes", notes_safe)):
        if not text:
            continue
        ok, _refusal = scan_input(text)
        if not ok:
            logger.warning("Email draft blocked: injection in %s", label)
            return EmailDraftResult(
                ok=False,
                error=f"Rejected: unsafe content in {label}",
            )

    user_prompt = build_email_user_prompt(
        lead_name=lead_name,
        business_name=business_name,
        lead_email=lead_email,
        company_display_name=company_display_name,
        calendly_link=calendly_link,
        intent=intent_safe,
        notes=notes_safe or None,
        conversation_excerpt=conversation_excerpt,
        rag_context=rag_context,
    )
    user_prompt = redact_pii(user_prompt)

    # Temporarily adapt system prompt for email (restore after call)
    original_prompt = getattr(gemini_client, "_system_prompt", "") or ""
    gemini_client._system_prompt = (
        original_prompt.rstrip()
        + "\n\n"
        + _EMAIL_CHANNEL_ADAPTATION
    )

    try:
        raw = gemini_client.generate_response_with_history([], user_prompt)
    except Exception as e:
        logger.error("Email AI generation failed: %s", e)
        gemini_client._system_prompt = original_prompt
        return EmailDraftResult(ok=False, error="AI generation failed")
    finally:
        gemini_client._system_prompt = original_prompt

    if not raw or not str(raw).strip():
        return EmailDraftResult(ok=False, error="AI returned empty draft")

    subject, body_text = _parse_subject_body(str(raw))
    if not subject or not body_text:
        return EmailDraftResult(
            ok=False,
            error="AI returned unparseable draft (expected JSON subject + body_text)",
            raw_model_output=str(raw)[:2000],
        )

    subject = subject.strip()
    body_text = body_text.strip()
    if len(subject) > 500:
        subject = subject[:497] + "..."
    if len(body_text) > 100_000:
        body_text = body_text[:99997] + "..."

    # Confidence on subject + body (URLs checked against adapted system context)
    combined = f"{subject}\n\n{body_text}"
    allowed_prompt = original_prompt + "\n" + (calendly_link or "")
    confidence = score_confidence(combined, allowed_prompt)

    # Email-specific floor: body should not be a one-liner chat reply
    if len(body_text) < 40:
        confidence = min(confidence, 0.45)

    tokens = estimate_tokens(user_prompt) + estimate_tokens(str(raw))
    rag_n = 0
    if rag_context:
        rag_n = max(1, rag_context.count("---"))

    if confidence < confidence_threshold:
        return EmailDraftResult(
            ok=False,
            subject=subject,
            body_text=body_text,
            confidence=confidence,
            error=(
                f"Low confidence draft ({confidence:.2f} < {confidence_threshold}). "
                "Review and edit before sending."
            ),
            raw_model_output=str(raw)[:2000],
            tokens_estimate=tokens,
            rag_chunks_used=rag_n,
        )

    return EmailDraftResult(
        ok=True,
        subject=subject,
        body_text=body_text,
        confidence=confidence,
        tokens_estimate=tokens,
        rag_chunks_used=rag_n,
    )

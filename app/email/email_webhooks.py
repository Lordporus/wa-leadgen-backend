"""
Resend (Svix) webhook verification + event handling — Phase E3.

Verify signatures with raw body + svix-* headers BEFORE parsing business logic.
No extra deps: HMAC-SHA256 manual verify (Svix protocol).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import func

from app.core.config import RESEND_WEBHOOK_SECRET
from app.core.database import SessionLocal, is_configured
from app.core.models import EmailSuppression, Lead, Message

logger = logging.getLogger(__name__)

# Reject webhooks older/newer than this to limit replay windows.
_SVIX_TOLERANCE_SECONDS = 5 * 60

# Map Resend event type → message.status
_STATUS_BY_EVENT = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.delivery_delayed": "delayed",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.failed": "failed",
    "email.opened": "opened",
    "email.clicked": "clicked",
    "email.suppressed": "suppressed",
}

# Inbound (body fetched separately via Receiving API) — Phase E6
_INBOUND_EVENTS = frozenset({"email.received"})

# Events that write email_suppressions + lead.email_status
_SUPPRESS_EVENTS = {
    "email.bounced": "bounce",
    "email.complained": "complaint",
    "email.suppressed": "bounce",
}


def verify_resend_signature(
    payload_body: bytes,
    *,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    secret: str | None = None,
    tolerance_seconds: int = _SVIX_TOLERANCE_SECONDS,
) -> bool:
    """
    Verify Resend/Svix webhook signature.

    signed_content = "{svix-id}.{svix-timestamp}.{body}"
    secret = base64(decode whsec_…)
    expected = base64(HMAC-SHA256(secret, signed_content))
    svix-signature is space-delimited "v1,<sig>" entries.
    """
    secret = (secret if secret is not None else RESEND_WEBHOOK_SECRET) or ""
    if not secret or not svix_id or not svix_timestamp or not svix_signature:
        return False

    try:
        ts = int(svix_timestamp)
    except (TypeError, ValueError):
        return False

    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        logger.warning(
            "Resend webhook timestamp outside tolerance: ts=%s now=%s",
            ts,
            now,
        )
        return False

    try:
        secret_b64 = secret[len("whsec_") :] if secret.startswith("whsec_") else secret
        key = base64.b64decode(secret_b64)
    except Exception:
        logger.error("RESEND_WEBHOOK_SECRET is not valid base64 (whsec_…)")
        return False

    if isinstance(payload_body, bytes):
        body_str = payload_body.decode("utf-8")
    else:
        body_str = str(payload_body)

    to_sign = f"{svix_id}.{svix_timestamp}.{body_str}".encode("utf-8")
    expected = base64.b64encode(
        hmac.new(key, to_sign, hashlib.sha256).digest()
    ).decode("ascii")

    for part in svix_signature.split(" "):
        part = part.strip()
        if not part or "," not in part:
            continue
        version, sig = part.split(",", 1)
        if version != "v1":
            continue
        if hmac.compare_digest(expected, sig):
            return True
    return False


def _extract_recipients(data: dict[str, Any]) -> list[str]:
    to = data.get("to") or []
    if isinstance(to, str):
        to = [to]
    out: list[str] = []
    for addr in to:
        if not addr:
            continue
        out.append(str(addr).strip().lower())
    return out


def _extract_client_id_from_tags(data: dict[str, Any]) -> int | None:
    tags = data.get("tags") or []
    # Resend may send tags as list of {name, value} or as a dict
    if isinstance(tags, dict):
        raw = tags.get("client_id")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return None
    if isinstance(tags, list):
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            name = tag.get("name") or tag.get("Name")
            if name == "client_id":
                try:
                    return int(tag.get("value") or tag.get("Value"))
                except (TypeError, ValueError):
                    return None
    return None


def _upsert_suppression(
    session,
    *,
    client_id: int,
    email: str,
    reason: str,
) -> None:
    email_norm = email.strip().lower()
    existing = (
        session.query(EmailSuppression)
        .filter(
            EmailSuppression.client_id == client_id,
            EmailSuppression.email == email_norm,
        )
        .first()
    )
    if existing:
        # Keep the first reason unless upgrading to complaint
        if reason == "complaint" and existing.reason != "complaint":
            existing.reason = reason
        return
    session.add(
        EmailSuppression(
            client_id=client_id,
            email=email_norm,
            reason=reason,
            created_at=datetime.utcnow(),
        )
    )


def _update_leads_email_status(
    session,
    *,
    client_id: int,
    email: str,
    email_status: str,
) -> int:
    email_norm = email.strip().lower()
    leads = (
        session.query(Lead)
        .filter(
            Lead.client_id == client_id,
            func.lower(Lead.email) == email_norm,
        )
        .all()
    )
    for lead in leads:
        lead.email_status = email_status
        lead.updated_at = datetime.utcnow()
    return len(leads)


def _find_message(session, provider_message_id: str | None) -> Message | None:
    if not provider_message_id:
        return None
    return (
        session.query(Message)
        .filter(
            Message.provider_message_id == provider_message_id,
            Message.channel == "email",
        )
        .first()
    )


def _resolve_client_id(
    session,
    data: dict[str, Any],
    message: Message | None,
) -> int | None:
    cid = _extract_client_id_from_tags(data)
    if cid is not None:
        return cid
    if message is not None:
        lead = session.get(Lead, message.lead_id)
        if lead:
            return lead.client_id
    return None


def handle_resend_event(event: dict[str, Any]) -> str:
    """
    Process a verified Resend webhook event payload.

    Returns a short result code for logging / response.
    """
    event_type = event.get("type") or event.get("event") or ""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        data = {}

    if not is_configured() or not SessionLocal:
        return "db_not_configured"

    provider_id = data.get("email_id") or data.get("id")
    recipients = _extract_recipients(data)
    new_status = _STATUS_BY_EVENT.get(event_type)

    # Phase E6: inbound receiving
    if event_type in _INBOUND_EVENTS:
        try:
            from email_inbound import process_inbound_email_event

            result = process_inbound_email_event(data)
            logger.info(
                "Resend inbound handled: code=%s lead=%s",
                result.code,
                result.lead_id,
            )
            return f"inbound:{result.code}"
        except Exception as e:
            logger.error("Resend inbound processing failed: %s", e, exc_info=True)
            return "inbound_error"

    # Opens/clicks optional — still update message status if we track them
    if event_type not in _STATUS_BY_EVENT and not event_type.startswith("email."):
        logger.info("Resend webhook ignored (non-email event): %s", event_type)
        return "ignored"

    if event_type not in _STATUS_BY_EVENT:
        logger.info("Resend webhook ignored (unhandled email event): %s", event_type)
        return "ignored"

    with SessionLocal() as session:
        message = _find_message(session, provider_id)
        client_id = _resolve_client_id(session, data, message)

        # Update message delivery status when we can match the send
        if message and new_status:
            # Don't regress a terminal failure to "opened" etc. lightly —
            # still record engagement events for analytics in provider_metadata.
            terminal = {"bounced", "complained", "failed", "suppressed"}
            if message.status in terminal and new_status in {"opened", "clicked", "delivered", "sent"}:
                pass
            else:
                message.status = new_status

            meta = dict(message.provider_metadata or {})
            meta["last_event"] = event_type
            meta["last_event_at"] = datetime.utcnow().isoformat() + "Z"
            if event_type == "email.bounced":
                meta["bounce"] = data.get("bounce") or data.get("failed") or {}
            message.provider_metadata = meta

        suppress_reason = _SUPPRESS_EVENTS.get(event_type)
        if suppress_reason:
            if client_id is None:
                # Fall back: suppress by matching any lead email for recipients
                # without client_id is unsafe multi-tenant → only act if we
                # can resolve tenant.
                logger.warning(
                    "Resend %s: cannot resolve client_id (email_id=%s); skip suppress",
                    event_type,
                    provider_id,
                )
                session.commit()
                return "no_client_id"

            email_status = (
                "complained"
                if suppress_reason == "complaint"
                else "bounced"
            )
            for addr in recipients:
                _upsert_suppression(
                    session,
                    client_id=client_id,
                    email=addr,
                    reason=suppress_reason,
                )
                _update_leads_email_status(
                    session,
                    client_id=client_id,
                    email=addr,
                    email_status=email_status,
                )
            session.commit()
            logger.info(
                "Resend %s handled: client=%s recipients=%s email_id=%s",
                event_type,
                client_id,
                recipients,
                provider_id,
            )
            return f"suppressed:{suppress_reason}"

        session.commit()
        logger.info(
            "Resend %s handled: message=%s status=%s",
            event_type,
            getattr(message, "id", None),
            new_status,
        )
        return f"status:{new_status or 'ok'}"


def parse_event_json(payload_body: bytes) -> dict[str, Any]:
    return json.loads(payload_body)

"""Phase 6 durable WhatsApp outbound intent and provider-status state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from requests.exceptions import RequestException
from sqlalchemy.exc import IntegrityError

from app.core import database
from app.core.config import WHATSAPP_OUTBOUND_ENABLED, WHATSAPP_SESSION_WINDOW_SECONDS
from app.core.models import Lead, Message, WhatsAppOutboundIntent, WhatsAppWebhookEvent


class OutboundIntentError(RuntimeError):
    """An outbound operation cannot safely continue."""


@dataclass(frozen=True)
class DispatchResult:
    state: str
    provider_message_id: str | None = None
    newly_sent: bool = False


_STATUS_ORDER = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}


def create_or_get_intent(
    *, client_id: int, inbound_provider_event_id: str, recipient_phone: str,
    correlation_id: str | None, reply_version: int = 1,
) -> int:
    """Create the one reply intent for an inbound event, tolerating DB races."""
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        event = session.query(WhatsAppWebhookEvent).filter_by(
            client_id=client_id, event_kind="message", event_id=inbound_provider_event_id
        ).one_or_none()
        if event is None:
            raise OutboundIntentError("Inbound WhatsApp receipt is missing or belongs to another tenant")
        existing = session.query(WhatsAppOutboundIntent).filter_by(
            client_id=client_id, inbound_event_id=event.id, reply_version=reply_version
        ).one_or_none()
        if existing is not None:
            return existing.id
        intent = WhatsAppOutboundIntent(
            client_id=client_id, inbound_event_id=event.id, reply_version=reply_version,
            recipient_phone=recipient_phone, correlation_id=correlation_id, state="pending",
        )
        session.add(intent)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            intent = session.query(WhatsAppOutboundIntent).filter_by(
                client_id=client_id, inbound_event_id=event.id, reply_version=reply_version
            ).one()
        return intent.id


def claim_for_generation(*, intent_id: int, client_id: int) -> str:
    """Atomically claim a new intent before any reply text is generated."""
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one_or_none()
        if intent is None:
            raise OutboundIntentError("Outbound intent does not belong to this tenant")
        if intent.state == "generating" and intent.body:
            return "dispatch"
        if intent.state not in {"pending", "generating"}:
            return "skip"
        intent.state = "generating"
        intent.claimed_at = datetime.utcnow()
        session.commit()
        return "generate"


def intent_body(*, intent_id: int, client_id: int) -> tuple[str, str]:
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).one()
        if not intent.body:
            raise OutboundIntentError("Outbound intent has no persisted reply text")
        return intent.recipient_phone, intent.body


def record_inbound_opt_out(*, client_id: int, recipient_phone: str, text: str) -> None:
    if text.strip().lower() not in {"stop", "unsubscribe", "cancel", "opt out", "opt-out"} or database.SessionLocal is None:
        return
    with database.SessionLocal() as session:
        lead = session.query(Lead).filter_by(client_id=client_id, phone=recipient_phone).with_for_update().one_or_none()
        if lead is not None:
            lead.whatsapp_opted_out_at = datetime.utcnow()
            session.commit()


def set_generated_body(*, intent_id: int, client_id: int, body: str) -> None:
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one()
        if intent.state != "generating":
            raise OutboundIntentError("Outbound intent is not awaiting generated content")
        intent.body = body
        session.commit()


def dispatch_intent(*, intent_id: int, client_id: int, sender: Callable[[str, str], str | None]) -> DispatchResult:
    """Claim, send, and persist one intent; never resend an uncertain outcome."""
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one_or_none()
        if intent is None:
            raise OutboundIntentError("Outbound intent does not belong to this tenant")
        if intent.state == "sent":
            return DispatchResult("sent", intent.provider_message_id, False)
        if intent.state in {"sending", "unknown"}:
            # A process may have died after Meta accepted the message.  Never
            # issue another send until an operator reconciles this intent.
            if intent.state == "sending":
                intent.state = "unknown"
                intent.failure_category = "uncertain_send_outcome"
                intent.failure_reason = "Worker resumed after provider send claim"
                session.commit()
            return DispatchResult("unknown")
        if intent.state != "generating" or not intent.body:
            return DispatchResult(intent.state)
        lead = session.query(Lead).filter_by(client_id=client_id, phone=intent.recipient_phone).with_for_update().one_or_none()
        if lead is None:
            raise OutboundIntentError("Outbound recipient is not a tenant lead")
        event = session.query(WhatsAppWebhookEvent).filter_by(id=intent.inbound_event_id, client_id=client_id).one()
        timestamp = event.payload.get("timestamp") if isinstance(event.payload, dict) else None
        try:
            session_open = isinstance(timestamp, str) and datetime.utcnow().timestamp() - int(timestamp) <= WHATSAPP_SESSION_WINDOW_SECONDS
        except (TypeError, ValueError):
            session_open = False
        if not WHATSAPP_OUTBOUND_ENABLED or lead.is_human_takeover or lead.whatsapp_opted_out_at or not session_open:
            intent.state = "blocked"
            intent.failure_category = "policy_blocked"
            intent.failure_reason = "Outbound disabled, opt-out/takeover active, or no open session/template policy"
            session.commit()
            return DispatchResult("blocked")
        intent.state = "sending"
        intent.attempt_count += 1
        intent.claimed_at = datetime.utcnow()
        recipient, body = intent.recipient_phone, intent.body
        session.commit()

    try:
        provider_message_id = sender(recipient, body)
    except RequestException as exc:
        _record_send_failure(intent_id, client_id, exc, uncertain=_is_uncertain_provider_error(exc))
        raise
    except Exception as exc:
        _record_send_failure(intent_id, client_id, exc, uncertain=False)
        raise
    if not provider_message_id:
        exc = OutboundIntentError("WhatsApp provider did not accept the outbound message")
        _record_send_failure(intent_id, client_id, exc, uncertain=False)
        raise exc

    # This transaction is the durable boundary after a provider response. If it
    # fails, a retry observes `sending` and moves to `unknown`, never resending.
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one()
        intent.provider_message_id = provider_message_id
        intent.provider_status = "sent"
        intent.state = "sent"
        intent.sent_at = datetime.utcnow()
        lead = session.query(Lead).filter_by(client_id=client_id, phone=intent.recipient_phone).one()
        if session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none() is None:
            session.add(Message(
                lead_id=lead.id, direction="OUTBOUND", msg_type="text", body=intent.body,
                wa_message_id=provider_message_id, channel="whatsapp", status="sent",
                outbound_intent_id=intent.id,
            ))
        session.commit()
    return DispatchResult("sent", provider_message_id, True)


def _record_send_failure(intent_id: int, client_id: int, error: Exception, *, uncertain: bool) -> None:
    if database.SessionLocal is None:
        return
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one_or_none()
        if intent is None:
            return
        intent.state = "unknown" if uncertain else "failed"
        intent.failure_category = "uncertain_send_outcome" if uncertain else type(error).__name__
        intent.failure_reason = str(error)[:2000]
        session.commit()


def _is_uncertain_provider_error(error: RequestException) -> bool:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return response is None or status is None or status >= 500


def apply_provider_status(*, client_id: int, provider_message_id: str, status: str) -> bool:
    """Apply a status only to a same-tenant known intent, monotonically."""
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    normalized = status.strip().lower()
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(
            client_id=client_id, provider_message_id=provider_message_id
        ).with_for_update().one_or_none()
        if intent is None:
            return False
        current = (intent.provider_status or "pending").lower()
        if _STATUS_ORDER.get(normalized, -1) > _STATUS_ORDER.get(current, -1):
            intent.provider_status = normalized
            intent.status_updated_at = datetime.utcnow()
            message = session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none()
            if message is not None:
                message.status = normalized
            session.commit()
        return True


def process_outbound_intent(*, intent_id: int, client_id: int) -> DispatchResult:
    from app.api.runtime import store, whatsapp
    recipient, body = intent_body(intent_id=intent_id, client_id=client_id)
    result = dispatch_intent(intent_id=intent_id, client_id=client_id, sender=whatsapp.send_message)
    if result.newly_sent and result.provider_message_id:
        store.append_message(recipient, direction="outbound", message=body, msg_type="text", wa_message_id=result.provider_message_id, client_id=client_id)
    return result


def replay_outbound_intent(*, intent_id: int, client_id: int) -> str:
    """Operator replay resumes the same failed intent; unknown sends stay blocked."""
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one_or_none()
        if intent is None or intent.state != "failed" or not intent.body:
            raise ValueError("Only failed intents with persisted reply text can be replayed")
        intent.state = "generating"
        intent.failure_category = None
        intent.failure_reason = None
        session.commit()
    from app.api.runtime import webhook_queue
    if webhook_queue is None:
        raise OutboundIntentError("WhatsApp replay queue is unavailable")
    return webhook_queue.enqueue(process_outbound_intent, intent_id=intent_id, client_id=client_id).id

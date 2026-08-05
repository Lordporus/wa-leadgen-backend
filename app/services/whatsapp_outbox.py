"""Phase 6 durable WhatsApp outbound intent and provider-status state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from requests.exceptions import RequestException
from sqlalchemy.exc import IntegrityError

from app.clients.whatsapp_client import MetaTransportError
from app.core import database
from app.core.models import Client, Lead, Message, WhatsAppOperatorAction, WhatsAppOutboundIntent, WhatsAppWebhookEvent
from app.services.whatsapp_observability import correlation_context
from app.services import whatsapp_policy


class OutboundIntentError(RuntimeError):
    """An outbound operation cannot safely continue."""


@dataclass(frozen=True)
class DispatchResult:
    state: str
    provider_message_id: str | None = None
    newly_sent: bool = False


_STATUS_ORDER = {"pending": 0, "sent": 1, "delivered": 2, "read": 3}


def _durable_correlation_locked(session: Any, intent: WhatsAppOutboundIntent) -> str:
    event = session.query(WhatsAppWebhookEvent).filter_by(
        id=intent.inbound_event_id,
        client_id=intent.client_id,
    ).one_or_none()
    correlation_id = str(getattr(event, "correlation_id", "") or "").strip()
    if not correlation_id:
        raise OutboundIntentError("Outbound intent has no durable correlation ID")
    current = str(intent.correlation_id or "").strip()
    if current and current != correlation_id:
        raise OutboundIntentError("Outbound intent correlation does not match its receipt")
    if not current:
        intent.correlation_id = correlation_id
    return correlation_id


def _next_provider_status(current: str, incoming: str) -> str | None:
    """Return a monotonic next status, with failure terminal once accepted."""
    if current == "failed":
        return None
    if incoming == "failed":
        return "failed" if current in {"pending", "sent"} else None
    if incoming not in _STATUS_ORDER:
        return None
    if _STATUS_ORDER.get(incoming, -1) > _STATUS_ORDER.get(current, -1):
        return incoming
    return None


def _mark_blocked_locked(
    session: Any,
    intent: WhatsAppOutboundIntent,
    *,
    category: str,
    reason: str,
) -> None:
    """Keep intent, timeline, and operator audit in one terminal state."""
    intent.state = "blocked"
    intent.failure_category = category
    intent.failure_reason = reason
    message = session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none()
    if message is not None:
        message.status = "blocked"
    if intent.operator_action_id is not None:
        action_row = session.query(WhatsAppOperatorAction).filter_by(
            id=intent.operator_action_id,
            client_id=intent.client_id,
        ).one_or_none()
        if action_row is not None:
            action_row.outcome = "blocked"
            action_row.completed_at = datetime.utcnow()


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
            _durable_correlation_locked(session, existing)
            session.commit()
            return existing.id
        durable_correlation = str(event.correlation_id or "").strip()
        if not durable_correlation or (correlation_id and correlation_id != durable_correlation):
            raise OutboundIntentError("Outbound correlation does not match the inbound receipt")
        lead = session.query(Lead).filter_by(client_id=client_id, phone=recipient_phone).with_for_update().one_or_none()
        if lead is None:
            raise OutboundIntentError("Outbound recipient is not a tenant lead")
        intent = WhatsAppOutboundIntent(
            client_id=client_id, inbound_event_id=event.id, reply_version=reply_version,
            recipient_phone=recipient_phone, correlation_id=durable_correlation, state="pending",
            intent_kind="ai_reply", takeover_version=lead.takeover_version,
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


def record_inbound_opt_out(
    *, client_id: int, recipient_phone: str, text: str,
    inbound_event_id: str | None = None,
) -> bool:
    return whatsapp_policy.record_inbound_opt_out(
        client_id=client_id,
        phone=recipient_phone,
        text=text,
        inbound_event_id=inbound_event_id,
    )


def set_generated_body(*, intent_id: int, client_id: int, body: str) -> None:
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one()
        if intent.state != "generating":
            raise OutboundIntentError("Outbound intent is not awaiting generated content")
        intent.body = body
        session.commit()


def dispatch_intent(
    *,
    intent_id: int,
    client_id: int,
    sender: Callable[..., str | None],
    final_guard: Callable[[Any, Client, Lead | None], str | None] | None = None,
    action: str = "queued_reply_send",
    allow_human_takeover: bool = False,
) -> DispatchResult:
    """Claim, policy-check, send, and persist once without unsafe retries."""
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
        # Persist the uncertain-send boundary before the provider call. A
        # crash after this commit remains non-retryable, preserving Phase 6.
        intent.state = "sending"
        intent.attempt_count += 1
        intent.claimed_at = datetime.utcnow()
        session.commit()

    provider_accepted = False
    decision: whatsapp_policy.PolicyDecision | None = None
    try:
        # This second transaction locks tenant + lead through the provider
        # call. Inbound opt-out uses the same locks, so the final policy check
        # and opt-out are serialized without weakening Phase 6 idempotency.
        with database.SessionLocal() as session:
            intent = session.query(WhatsAppOutboundIntent).filter_by(
                id=intent_id, client_id=client_id
            ).with_for_update().one()
            client = session.query(Client).filter_by(id=client_id).with_for_update().one()
            lead = session.query(Lead).filter_by(
                client_id=client_id, phone=intent.recipient_phone
            ).with_for_update().one_or_none()
            if lead is None:
                raise OutboundIntentError("Outbound recipient is not a tenant lead")
            if intent.intent_kind == "ai_reply":
                from app.services import whatsapp_operations

                if not whatsapp_operations.enabled_locked(
                    session,
                    whatsapp_operations.AI_AUTO_REPLY,
                    client_id=client_id,
                    lock=True,
                ):
                    _mark_blocked_locked(
                        session,
                        intent,
                        category="ai_auto_reply_disabled",
                        reason="Tenant AI auto-reply operational control is disabled",
                    )
                    from app.services import ai_decision

                    ai_decision.mark_audit_outcome_locked(
                        session,
                        audit_id=intent.ai_decision_audit_id,
                        client_id=client_id,
                        outcome="blocked",
                        reason="ai_auto_reply_disabled",
                    )
                    session.commit()
                    return DispatchResult("blocked")
            if intent.takeover_version is None or intent.takeover_version != lead.takeover_version:
                _mark_blocked_locked(
                    session, intent, category="stale_takeover_version",
                    reason="Conversation ownership changed after intent creation",
                )
                from app.services import ai_decision
                ai_decision.mark_audit_outcome_locked(
                    session, audit_id=intent.ai_decision_audit_id,
                    client_id=client_id, outcome="blocked",
                    reason="stale_takeover_version",
                )
                session.commit()
                return DispatchResult("blocked")
            if intent.intent_kind == "manual" and not lead.is_human_takeover:
                _mark_blocked_locked(
                    session, intent, category="manual_takeover_released",
                    reason="Manual send requires active takeover",
                )
                session.commit()
                return DispatchResult("blocked")
            credentials = whatsapp_policy.tenant_meta_credentials(client)
            decision = whatsapp_policy.evaluate_locked(
                session,
                client=client,
                lead=lead,
                action=action,
                message_type="text",
                outbound_intent_id=intent.id,
                correlation_id=intent.correlation_id,
                credentials=credentials,
                allow_human_takeover=allow_human_takeover,
            )
            if not decision.allowed:
                _mark_blocked_locked(
                    session, intent, category="policy_blocked",
                    reason=decision.reason_code,
                )
                whatsapp_policy.set_provider_audit_outcome(
                    session,
                    decision,
                    outcome="blocked",
                )
                from app.services import ai_decision
                ai_decision.mark_audit_outcome_locked(session, audit_id=intent.ai_decision_audit_id, client_id=client_id, outcome="blocked", reason=decision.reason_code)
                session.commit()
                return DispatchResult("blocked")

            if final_guard is not None:
                guard_reason = final_guard(session, client, lead)
                if guard_reason:
                    _mark_blocked_locked(
                        session, intent, category="final_ai_guard",
                        reason=guard_reason,
                    )
                    whatsapp_policy.set_provider_audit_outcome(session, decision, outcome="blocked", failure_category=guard_reason)
                    from app.services import ai_decision
                    ai_decision.mark_audit_outcome_locked(session, audit_id=intent.ai_decision_audit_id, client_id=client_id, outcome="blocked", reason=guard_reason)
                    session.commit()
                    return DispatchResult("blocked")

            if not intent.body:
                raise OutboundIntentError("Outbound intent lost its persisted reply text")
            session.flush()
            provider_message_id = sender(
                intent.recipient_phone,
                intent.body,
                credentials=credentials,
            )
            if not provider_message_id:
                raise OutboundIntentError("WhatsApp provider did not accept the outbound message")
            provider_accepted = True

            intent.provider_message_id = provider_message_id
            intent.provider_status = "sent"
            intent.state = "sent"
            intent.sent_at = datetime.utcnow()
            whatsapp_policy.set_provider_audit_outcome(
                session,
                decision,
                outcome="accepted",
            )
            from app.services import ai_decision
            ai_decision.mark_audit_outcome_locked(session, audit_id=intent.ai_decision_audit_id, client_id=client_id, outcome="sent")
            message = session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none()
            if message is None:
                session.add(Message(
                    lead_id=lead.id, direction="OUTBOUND", msg_type="text", body=intent.body,
                    wa_message_id=provider_message_id, channel="whatsapp", status="sent",
                    outbound_intent_id=intent.id,
                ))
            else:
                message.status = "sent"
                message.wa_message_id = provider_message_id
                message.provider_message_id = provider_message_id
            if intent.operator_action_id is not None:
                action_row = session.query(WhatsAppOperatorAction).filter_by(
                    id=intent.operator_action_id, client_id=client_id
                ).one_or_none()
                if action_row is not None:
                    action_row.outcome = "sent"
                    action_row.completed_at = datetime.utcnow()
            session.commit()
    except Exception as exc:
        if decision is not None and decision.allowed:
            whatsapp_policy.persist_policy_decision(
                decision,
                provider_outcome=(
                    "accepted_uncommitted" if provider_accepted else "failed"
                ),
                failure_category=whatsapp_policy.classify_provider_failure(
                    exc,
                    provider_accepted=provider_accepted,
                ),
            )
        _record_send_failure(
            intent_id,
            client_id,
            exc,
            uncertain=_send_failure_is_uncertain(exc, provider_accepted=provider_accepted),
        )
        from app.services import ai_decision
        ai_decision.record_intent_outcome(audit_id=getattr(intent, "ai_decision_audit_id", None), client_id=client_id, outcome="failed", reason=type(exc).__name__)
        raise
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
        message = session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none()
        if message is not None:
            message.status = intent.state
        if intent.operator_action_id is not None:
            action = session.query(WhatsAppOperatorAction).filter_by(
                id=intent.operator_action_id, client_id=client_id
            ).one_or_none()
            if action is not None:
                action.outcome = intent.state
                action.completed_at = datetime.utcnow()
        session.commit()


def _is_uncertain_provider_error(error: RequestException) -> bool:
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return response is None or status is None or status >= 500


def _send_failure_is_uncertain(error: Exception, *, provider_accepted: bool) -> bool:
    if provider_accepted:
        return True
    if isinstance(error, MetaTransportError):
        return True
    return isinstance(error, RequestException) and _is_uncertain_provider_error(error)


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
        next_status = _next_provider_status(current, normalized)
        if next_status is not None:
            intent.provider_status = next_status
            intent.status_updated_at = datetime.utcnow()
            message = session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none()
            if message is not None:
                message.status = next_status
            session.commit()
        return True


def process_outbound_intent(*, intent_id: int, client_id: int) -> DispatchResult:
    from app.api.runtime import store, whatsapp
    from app.services import ai_decision
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one()
        if not intent.body:
            raise OutboundIntentError("Outbound intent has no persisted reply text")
        recipient, body, audit_id = intent.recipient_phone, intent.body, intent.ai_decision_audit_id
        intent_kind = intent.intent_kind
        correlation_id = _durable_correlation_locked(session, intent)
        session.commit()
    def guard(session, client, lead):
        return ai_decision.durable_reply_guard(
            session,
            client,
            lead,
            audit_id=audit_id,
            intent_id=intent_id,
            body=body,
        )
    is_manual = intent_kind == "manual"
    with correlation_context(correlation_id, tenant_id=client_id):
        result = dispatch_intent(
            intent_id=intent_id,
            client_id=client_id,
            sender=whatsapp.send_message,
            final_guard=None if is_manual else guard,
            action="human_manual_send" if is_manual else "queued_reply_send",
            allow_human_takeover=is_manual,
        )
    if result.newly_sent and result.provider_message_id:
        store.append_message(recipient, direction="outbound", message=body, msg_type="human" if is_manual else "text", wa_message_id=result.provider_message_id, client_id=client_id)
    return result


def replay_outbound_intent(*, intent_id: int, client_id: int) -> str:
    """Operator replay resumes the same failed intent; unknown sends stay blocked."""
    if database.SessionLocal is None:
        raise OutboundIntentError("WhatsApp outbox requires the durable database")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one_or_none()
        if intent is None or intent.state != "failed" or not intent.body:
            raise ValueError("Only failed intents with persisted reply text can be replayed")
        _durable_correlation_locked(session, intent)
        intent.state = "generating"
        intent.failure_category = None
        intent.failure_reason = None
        session.commit()
    from app.api.runtime import webhook_queue
    if webhook_queue is None:
        raise OutboundIntentError("WhatsApp replay queue is unavailable")
    return webhook_queue.enqueue(process_outbound_intent, intent_id=intent_id, client_id=client_id).id

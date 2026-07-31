"""Phase 10 atomic takeover, manual outbox, timeline, and operator audit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core import database
from app.core.models import (
    Lead,
    Message,
    WhatsAppOperatorAction,
    WhatsAppOutboundIntent,
    WhatsAppTakeoverTask,
    WhatsAppWebhookEvent,
)


class InboxConflict(RuntimeError):
    pass


class InboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class TakeoverState:
    lead_id: int
    enabled: bool
    version: int
    owner: str | None
    reason: str | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def transition_takeover(
    *,
    client_id: int,
    lead_id: int,
    enabled: bool,
    expected_version: int | None,
    operator_id: str,
    reason: str,
    correlation_id: str,
    confirmed: bool,
) -> TakeoverState:
    """Version and audit takeover under the same lead lock used before sends."""
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    if not enabled and not confirmed:
        raise InboxConflict("release_confirmation_required")
    with database.SessionLocal() as session:
        lead = session.query(Lead).filter_by(id=lead_id, client_id=client_id).with_for_update().one_or_none()
        if lead is None:
            raise InboxConflict("lead_not_found")
        current_version = int(lead.takeover_version or 0)
        if expected_version is not None and current_version != expected_version:
            raise InboxConflict("stale_takeover_version")
        if bool(lead.is_human_takeover) == enabled:
            return TakeoverState(lead.id, enabled, current_version, lead.takeover_owner, lead.takeover_reason)

        next_version = current_version + 1
        now = _now()
        lead.is_human_takeover = enabled
        lead.takeover_version = next_version
        lead.updated_at = now
        if enabled:
            lead.takeover_owner = operator_id
            lead.takeover_reason = reason[:255]
            lead.takeover_at = now
            lead.released_at = None
        else:
            lead.released_at = now

        action = WhatsAppOperatorAction(
            client_id=client_id,
            lead_id=lead.id,
            operator_id=operator_id,
            action="takeover" if enabled else "release",
            correlation_id=correlation_id,
            reason=reason[:255],
            from_version=current_version,
            to_version=next_version,
            outcome="completed",
            completed_at=now,
        )
        session.add(action)

        if enabled:
            intents = session.query(WhatsAppOutboundIntent).filter(
                WhatsAppOutboundIntent.client_id == client_id,
                WhatsAppOutboundIntent.recipient_phone == lead.phone,
                WhatsAppOutboundIntent.intent_kind == "ai_reply",
                WhatsAppOutboundIntent.state.in_(("pending", "generating")),
            ).with_for_update().all()
            for intent in intents:
                intent.state = "blocked"
                intent.failure_category = "human_takeover"
                intent.failure_reason = "takeover_version_changed"
                message = session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none()
                if message is not None:
                    message.status = "blocked"
            session.add(WhatsAppTakeoverTask(
                client_id=client_id,
                lead_id=lead.id,
                takeover_version=next_version,
                reason=reason[:255] or "operator_takeover",
                status="open",
                owner=operator_id,
            ))
        else:
            for task in session.query(WhatsAppTakeoverTask).filter(
                WhatsAppTakeoverTask.client_id == client_id,
                WhatsAppTakeoverTask.lead_id == lead.id,
                WhatsAppTakeoverTask.status.in_(("open", "acknowledged")),
            ).with_for_update().all():
                task.status = "resolved"
                task.resolved_at = now
                task.updated_at = now
        session.commit()
        return TakeoverState(lead.id, enabled, next_version, lead.takeover_owner, lead.takeover_reason)


def create_manual_intent(
    *,
    client_id: int,
    lead_id: int,
    body: str,
    idempotency_key: str,
    operator_id: str,
    correlation_id: str,
) -> tuple[int, bool]:
    """Persist one manual message and its operator audit before provider work."""
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    with database.SessionLocal() as session:
        existing_action = session.query(WhatsAppOperatorAction).filter_by(
            client_id=client_id, idempotency_key=idempotency_key
        ).one_or_none()
        if existing_action is not None:
            if existing_action.action != "manual_send" or existing_action.lead_id != lead_id:
                raise InboxConflict("idempotency_key_conflict")
            intent = session.query(WhatsAppOutboundIntent).filter_by(
                id=existing_action.outbound_intent_id, client_id=client_id
            ).one()
            if intent.body != body:
                raise InboxConflict("idempotency_body_mismatch")
            return intent.id, False

        lead = session.query(Lead).filter_by(id=lead_id, client_id=client_id).with_for_update().one_or_none()
        if lead is None:
            raise InboxConflict("lead_not_found")
        if not lead.is_human_takeover:
            raise InboxConflict("manual_send_requires_takeover")
        event = WhatsAppWebhookEvent(
            client_id=client_id,
            event_kind="manual",
            event_id="manual:" + idempotency_key,
            correlation_id=correlation_id,
            phone_number_id="operator",
            payload={"source": "operator"},
            state="processed",
            processed_at=_now(),
        )
        action = WhatsAppOperatorAction(
            client_id=client_id,
            lead_id=lead.id,
            operator_id=operator_id,
            action="manual_send",
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            from_version=lead.takeover_version,
            to_version=lead.takeover_version,
            outcome="pending",
        )
        session.add_all([event, action])
        session.flush()
        intent = WhatsAppOutboundIntent(
            client_id=client_id,
            inbound_event_id=event.id,
            reply_version=1,
            recipient_phone=lead.phone,
            body=body,
            state="generating",
            correlation_id=correlation_id,
            intent_kind="manual",
            takeover_version=lead.takeover_version,
            operator_action_id=action.id,
        )
        session.add(intent)
        session.flush()
        action.outbound_intent_id = intent.id
        session.add(Message(
            lead_id=lead.id,
            direction="OUTBOUND",
            msg_type="human",
            body=body,
            status="pending",
            channel="whatsapp",
            outbound_intent_id=intent.id,
        ))
        session.commit()
        return intent.id, True


def timeline(*, client_id: int, lead_id: int) -> list[dict[str, Any]]:
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    with database.SessionLocal() as session:
        lead = session.query(Lead).filter_by(id=lead_id, client_id=client_id).one_or_none()
        if lead is None:
            raise InboxConflict("lead_not_found")
        messages = session.query(Message).filter_by(lead_id=lead.id).order_by(
            Message.created_at.asc(), Message.id.asc()
        ).all()
        intents = session.query(WhatsAppOutboundIntent).filter_by(
            client_id=client_id, recipient_phone=lead.phone
        ).all()
        by_intent = {intent.id: intent for intent in intents}
        seen: set[int] = set()
        rows: list[dict[str, Any]] = []
        for message in messages:
            intent = by_intent.get(message.outbound_intent_id) if message.outbound_intent_id else None
            if intent is not None:
                seen.add(intent.id)
            rows.append(_timeline_row(message, intent))
        for intent in intents:
            if intent.id not in seen and intent.body:
                rows.append(_timeline_row(None, intent))
        rows.sort(key=lambda row: (row["created_at"], row["id"]))
        return rows


def _timeline_row(message: Message | None, intent: WhatsAppOutboundIntent | None) -> dict[str, Any]:
    created = message.created_at if message is not None else intent.created_at  # type: ignore[union-attr]
    state = intent.state if intent is not None else (message.status or "received")  # type: ignore[union-attr]
    provider_status = intent.provider_status if intent is not None else message.status  # type: ignore[union-attr]
    if state in {"pending", "generating", "sending"}:
        visible_status = "pending" if state != "sending" else "unknown"
    elif state == "sent":
        visible_status = provider_status or "sent"
    else:
        visible_status = state
    return {
        "id": "m%s" % message.id if message is not None else "i%s" % intent.id,  # type: ignore[union-attr]
        "role": "user" if message is not None and message.direction == "INBOUND" else ("human" if (message and message.msg_type == "human") or (intent and intent.intent_kind == "manual") else "ai"),
        "content": (message.body if message is not None else intent.body) or "",  # type: ignore[union-attr]
        "timestamp": created.isoformat() if created else "",
        "created_at": created.isoformat() if created else "",
        "status": visible_status,
        "send_state": state,
        "provider_status": provider_status,
        "failure_category": intent.failure_category if intent is not None else None,
        "failure_reason": intent.failure_reason if intent is not None else None,
        "correlation_id": intent.correlation_id if intent is not None else None,
        "channel": (message.channel if message is not None else "whatsapp") or "whatsapp",
        "subject": message.subject if message is not None else None,
        "msg_type": message.msg_type if message is not None else ("human" if intent and intent.intent_kind == "manual" else "text"),
    }


def list_tasks(*, client_id: int) -> list[dict[str, Any]]:
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    now = _now()
    with database.SessionLocal() as session:
        tasks = session.query(WhatsAppTakeoverTask).filter(
            WhatsAppTakeoverTask.client_id == client_id,
            WhatsAppTakeoverTask.status.in_(("open", "acknowledged")),
        ).order_by(WhatsAppTakeoverTask.created_at.asc()).all()
        return [{
            "id": task.id, "lead_id": task.lead_id, "reason": task.reason,
            "status": task.status, "owner": task.owner,
            "age_seconds": max(0, int((now - task.created_at.replace(tzinfo=task.created_at.tzinfo or timezone.utc)).total_seconds())),
            "takeover_version": task.takeover_version,
        } for task in tasks]


def list_operator_actions(*, client_id: int, lead_id: int) -> list[dict[str, Any]]:
    """Return the content-minimised operator history for one tenant lead."""
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    with database.SessionLocal() as session:
        lead = session.query(Lead.id).filter_by(
            id=lead_id, client_id=client_id
        ).one_or_none()
        if lead is None:
            raise InboxConflict("lead_not_found")
        actions = session.query(WhatsAppOperatorAction).filter_by(
            client_id=client_id, lead_id=lead_id
        ).order_by(
            WhatsAppOperatorAction.created_at.desc(),
            WhatsAppOperatorAction.id.desc(),
        ).all()
        return [{
            "id": action.id,
            "operator_id": action.operator_id,
            "action": action.action,
            "correlation_id": action.correlation_id,
            "reason": action.reason,
            "from_version": action.from_version,
            "to_version": action.to_version,
            "outcome": action.outcome,
            "outbound_intent_id": action.outbound_intent_id,
            "created_at": action.created_at.isoformat() if action.created_at else "",
            "completed_at": action.completed_at.isoformat() if action.completed_at else None,
        } for action in actions]


def outbound_intent_state(*, client_id: int, intent_id: int) -> str | None:
    """Read one tenant-scoped intent state for an actionable API error."""
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    with database.SessionLocal() as session:
        row = session.query(WhatsAppOutboundIntent.state).filter_by(
            id=intent_id, client_id=client_id
        ).one_or_none()
        return row[0] if row is not None else None


def update_task(*, client_id: int, task_id: int, operator_id: str, resolve: bool, correlation_id: str) -> dict[str, Any]:
    if database.SessionLocal is None:
        raise InboxUnavailable("durable_database_unavailable")
    now = _now()
    with database.SessionLocal() as session:
        task = session.query(WhatsAppTakeoverTask).filter_by(id=task_id, client_id=client_id).with_for_update().one_or_none()
        if task is None:
            raise InboxConflict("task_not_found")
        if task.status == "resolved":
            if resolve:
                return {"id": task.id, "status": task.status, "owner": task.owner}
            raise InboxConflict("task_already_resolved")
        if task.status == "acknowledged" and not resolve:
            return {"id": task.id, "status": task.status, "owner": task.owner}
        if task.status not in {"open", "acknowledged"}:
            raise InboxConflict("invalid_task_state")
        task.owner = operator_id
        task.status = "resolved" if resolve else "acknowledged"
        if resolve:
            task.resolved_at = now
        else:
            task.acknowledged_at = now
        task.updated_at = now
        session.add(WhatsAppOperatorAction(
            client_id=client_id, lead_id=task.lead_id, operator_id=operator_id,
            action="resolve" if resolve else "acknowledge", correlation_id=correlation_id,
            reason=task.reason, from_version=task.takeover_version,
            to_version=task.takeover_version, outcome="completed", completed_at=now,
        ))
        session.commit()
        return {"id": task.id, "status": task.status, "owner": task.owner}

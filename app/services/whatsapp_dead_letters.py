"""Tenant-safe Phase 12C dead-letter inspection and bounded audited replay."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.core import database
from app.core.models import (
    Client,
    Lead,
    WhatsAppOperatorAction,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppWebhookEvent,
)
from app.core.whatsapp_phase12c import MAX_DEAD_LETTER_LIST, MAX_REPLAY_BATCH
from app.services import whatsapp_operations, whatsapp_policy


class DeadLetterError(RuntimeError):
    """A dead-letter operation cannot be completed safely."""


class DeadLetterConflict(DeadLetterError):
    """Replay state or correlation no longer matches the inspected receipt."""


def _factory():
    if database.SessionLocal is None:
        raise DeadLetterError("WhatsApp dead-letter operations require the durable database")
    return database.SessionLocal


def _error_type(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.split(":", 1)[0].strip()
    return candidate if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,119}", candidate) else "unclassified"


def _lead_for_receipt_locked(session: Any, receipt: WhatsAppWebhookEvent) -> Lead | None:
    phone: str | None = None
    if receipt.event_kind == "message":
        raw = receipt.payload.get("from") if isinstance(receipt.payload, dict) else None
        if isinstance(raw, str):
            try:
                phone = whatsapp_policy.normalize_phone(raw)
            except ValueError:
                phone = None
    elif receipt.event_kind == "status":
        intent = session.query(WhatsAppOutboundIntent).filter_by(
            client_id=receipt.client_id,
            provider_message_id=receipt.event_id,
        ).one_or_none()
        if intent is not None:
            phone = intent.recipient_phone
    if not phone:
        return None
    return session.query(Lead).filter_by(
        client_id=receipt.client_id,
        phone=phone,
    ).with_for_update().one_or_none()


def _eligible_locked(session: Any, receipt: WhatsAppWebhookEvent) -> bool:
    return (
        receipt.event_kind in {"message", "status"}
        and bool(receipt.correlation_id)
        and _lead_for_receipt_locked(session, receipt) is not None
    )


def list_dead_letters(*, client_id: int, limit: int) -> dict[str, Any]:
    if limit < 1 or limit > MAX_DEAD_LETTER_LIST:
        raise DeadLetterError("Dead-letter list limit is out of bounds")
    with _factory()() as session:
        rows = session.query(WhatsAppWebhookEvent).filter(
            WhatsAppWebhookEvent.client_id == client_id,
            WhatsAppWebhookEvent.state.in_(("dead_letter", "enqueue_failed", "replay_requested")),
        ).order_by(
            WhatsAppWebhookEvent.dead_lettered_at.desc(),
            WhatsAppWebhookEvent.id.desc(),
        ).limit(limit + 1).all()
        items = [
            {
                "receipt_id": row.id,
                "event_kind": row.event_kind,
                "correlation_id": row.correlation_id,
                "state": row.state,
                "attempt_count": row.attempt_count,
                "error_type": _error_type(row.last_error),
                "received_at": row.received_at,
                "dead_lettered_at": row.dead_lettered_at,
                "replay_eligible": _eligible_locked(session, row),
            }
            for row in rows[:limit]
        ]
        return {"items": items, "limit": limit, "truncated": len(rows) > limit}


def _controls_allow_replay_locked(session: Any, *, client_id: int, message: bool) -> bool:
    checks: list[tuple[str, int | None]] = [
        (whatsapp_operations.WORKER_CONSUMPTION, None),
        (whatsapp_operations.GLOBAL_OUTBOUND, None),
    ]
    if message:
        checks.extend([
            (whatsapp_operations.TENANT_OUTBOUND, client_id),
            (whatsapp_operations.AI_AUTO_REPLY, client_id),
        ])
    for control, scoped_client in checks:
        if not whatsapp_operations.enabled_locked(
            session,
            control,
            client_id=scoped_client,
            lock=True,
        ):
            return False
    return True


def _replay_one(
    *,
    client_id: int,
    receipt_id: int,
    original_correlation_id: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    job_id: str
    action_id: int
    with _factory()() as session:
        session.query(Client).filter_by(id=client_id).with_for_update().one()
        receipt = session.query(WhatsAppWebhookEvent).filter_by(
            id=receipt_id,
            client_id=client_id,
        ).with_for_update().one_or_none()
        if receipt is None:
            raise DeadLetterConflict("Dead-letter receipt not found for this tenant")
        if receipt.correlation_id != original_correlation_id:
            raise DeadLetterConflict("Original correlation ID does not match the receipt")
        lead = _lead_for_receipt_locked(session, receipt)
        if lead is None:
            raise DeadLetterConflict("Dead-letter receipt has no tenant-owned lead and is not replayable")
        idempotency_key = f"dead-letter:{receipt.id}:{receipt.attempt_count}"
        action = session.query(WhatsAppOperatorAction).filter_by(
            client_id=client_id,
            idempotency_key=idempotency_key,
        ).with_for_update().one_or_none()
        if action is not None and action.outcome == "queued":
            return {
                "receipt_id": receipt.id,
                "correlation_id": receipt.correlation_id,
                "state": "already_queued",
                "idempotent": True,
            }
        if receipt.state not in {"dead_letter", "enqueue_failed", "replay_requested"}:
            raise DeadLetterConflict("Receipt is no longer eligible for replay")
        if not _controls_allow_replay_locked(
            session,
            client_id=client_id,
            message=receipt.event_kind == "message",
        ):
            raise DeadLetterConflict("Operational controls currently block dead-letter replay")
        if receipt.event_kind == "message" and session.query(WhatsAppOptOut).filter_by(
            client_id=client_id,
            phone=lead.phone,
        ).one_or_none() is not None:
            raise DeadLetterConflict("WhatsApp policy blocks replay for an opted-out lead")
        if action is None:
            action = WhatsAppOperatorAction(
                client_id=client_id,
                lead_id=lead.id,
                operator_id=actor,
                action="dead_letter_replay",
                idempotency_key=idempotency_key,
                correlation_id=receipt.correlation_id,
                reason=reason,
                outcome="requested",
            )
            session.add(action)
            session.flush()
        elif action.correlation_id != receipt.correlation_id or action.reason != reason:
            raise DeadLetterConflict("Replay idempotency key was already used with different evidence")
        action.outcome = "requested"
        action.completed_at = None
        job_id = f"whatsapp-replay-{receipt.id}-{receipt.attempt_count}"
        receipt.state = "replay_requested"
        receipt.rq_job_id = job_id
        action_id = action.id
        session.commit()

    try:
        from app.services import whatsapp_queue

        queued_id = whatsapp_queue.enqueue_persisted_receipt(
            receipt_id=receipt_id,
            client_id=client_id,
            job_id=job_id,
        )
    except Exception as exc:
        with _factory()() as session:
            action = session.query(WhatsAppOperatorAction).filter_by(
                id=action_id,
                client_id=client_id,
            ).with_for_update().one()
            receipt = session.query(WhatsAppWebhookEvent).filter_by(
                id=receipt_id,
                client_id=client_id,
            ).with_for_update().one()
            action.outcome = "failed"
            action.completed_at = datetime.utcnow()
            receipt.state = "enqueue_failed"
            session.commit()
        raise DeadLetterError("Dead-letter replay could not be durably enqueued") from exc

    with _factory()() as session:
        action = session.query(WhatsAppOperatorAction).filter_by(
            id=action_id,
            client_id=client_id,
        ).with_for_update().one()
        receipt = session.query(WhatsAppWebhookEvent).filter_by(
            id=receipt_id,
            client_id=client_id,
        ).with_for_update().one()
        action.outcome = "queued"
        action.completed_at = datetime.utcnow()
        receipt.state = "queued"
        receipt.rq_job_id = queued_id
        session.commit()
        return {
            "receipt_id": receipt.id,
            "correlation_id": receipt.correlation_id,
            "state": "queued",
            "idempotent": False,
        }


def replay_dead_letters(
    *,
    client_id: int,
    items: list[tuple[int, str]],
    replay_limit: int,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    if not actor.strip() or not reason.strip():
        raise DeadLetterError("Trusted actor and reason are required")
    if replay_limit < 1 or replay_limit > MAX_REPLAY_BATCH or len(items) > replay_limit:
        raise DeadLetterError("Replay request exceeds its explicit bounded limit")
    if len({receipt_id for receipt_id, _ in items}) != len(items):
        raise DeadLetterError("Replay request contains duplicate receipt IDs")
    return {
        "outcomes": [
            _replay_one(
                client_id=client_id,
                receipt_id=receipt_id,
                original_correlation_id=correlation_id,
                actor=actor,
                reason=reason.strip(),
            )
            for receipt_id, correlation_id in items
        ]
    }

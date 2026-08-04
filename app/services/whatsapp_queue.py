"""Durable RQ ingress, worker execution, and dead-letter handling for WhatsApp."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from redis.exceptions import RedisError
from requests.exceptions import RequestException
from rq import Retry
from rq.job import get_current_job
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.runtime import webhook_queue
from app.core import database
from app.core.config import (
    WHATSAPP_RQ_JOB_TIMEOUT,
    WHATSAPP_RQ_MAX_RETRIES,
    WHATSAPP_RQ_RETRY_INTERVALS,
)
from app.core.models import WhatsAppWebhookEvent

logger = logging.getLogger(__name__)


class PermanentWebhookError(Exception):
    """An event that must be visible for replay, never retried automatically."""


_RETRYABLE_EXCEPTIONS = (RedisError, OperationalError, TimeoutError, ConnectionError)


def _event_id(kind: str, payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value.strip():
        raise PermanentWebhookError(f"WhatsApp {kind} event is missing provider id")
    return value.strip()


def enqueue_event(*, kind: str, payload: dict[str, Any], phone_number_id: str, client_id: int) -> str:
    """Persist a receipt, enqueue exactly once, and return its correlation id.

    Redis/RQ is the delivery mechanism; the receipt makes failed and exhausted
    work queryable and replayable without falling back to web-process work.
    """
    event_id = _event_id(kind, payload)
    if database.SessionLocal is None:
        raise RuntimeError("WhatsApp queue requires the durable database receipt store")
    if webhook_queue is None:
        raise RuntimeError("WhatsApp queue is unavailable")

    with database.SessionLocal() as session:
        receipt = (
            session.query(WhatsAppWebhookEvent)
            .filter_by(client_id=client_id, event_kind=kind, event_id=event_id)
            .one_or_none()
        )
        if receipt and receipt.state in {"queued", "processing", "processed"}:
            return receipt.correlation_id
        if receipt is None:
            receipt = WhatsAppWebhookEvent(
                client_id=client_id,
                event_kind=kind,
                event_id=event_id,
                correlation_id=str(uuid.uuid4()),
                phone_number_id=phone_number_id,
                payload=payload,
                state="received",
            )
            session.add(receipt)
            session.flush()
        else:
            receipt.payload = payload
            receipt.phone_number_id = phone_number_id
            receipt.state = "received"
            receipt.last_error = None
            receipt.dead_lettered_at = None
        try:
            session.commit()
        except IntegrityError:
            # Concurrent Meta delivery created the tenant-scoped receipt first.
            # Reconcile it rather than rejecting a valid duplicate webhook.
            session.rollback()
            existing = session.query(WhatsAppWebhookEvent).filter_by(
                client_id=client_id, event_kind=kind, event_id=event_id
            ).one()
            return existing.correlation_id

        envelope = {
            "event_id": receipt.event_id,
            "event_kind": receipt.event_kind,
            "tenant_id": receipt.client_id,
            "phone_number_id": receipt.phone_number_id,
            "correlation_id": receipt.correlation_id,
            "attempt": 0,
            "payload": receipt.payload,
        }
        try:
            job = webhook_queue.enqueue(
                process_webhook_event,
                envelope,
                job_timeout=WHATSAPP_RQ_JOB_TIMEOUT,
                retry=Retry(
                    max=WHATSAPP_RQ_MAX_RETRIES,
                    interval=list(WHATSAPP_RQ_RETRY_INTERVALS),
                ),
                meta={"whatsapp_initial_retries": WHATSAPP_RQ_MAX_RETRIES},
            )
        except Exception as exc:
            receipt.state = "enqueue_failed"
            receipt.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            session.commit()
            raise RuntimeError("WhatsApp event could not be durably enqueued") from exc

        receipt.state = "queued"
        receipt.rq_job_id = job.id
        session.commit()
        return receipt.correlation_id


def process_webhook_event(envelope: dict[str, Any]) -> None:
    """RQ worker entry point; all WhatsApp business work starts here."""
    kind = envelope.get("event_kind")
    event_id = envelope.get("event_id")
    tenant_id = envelope.get("tenant_id")
    phone_number_id = envelope.get("phone_number_id")
    payload = envelope.get("payload")
    if kind not in {"message", "status"} or not isinstance(event_id, str):
        raise PermanentWebhookError("Invalid WhatsApp job envelope")
    if not isinstance(tenant_id, int) or not isinstance(phone_number_id, str) or not isinstance(payload, dict):
        raise PermanentWebhookError("Incomplete WhatsApp job envelope")

    from app.services import whatsapp_operations

    if not whatsapp_operations.enabled(
        whatsapp_operations.WORKER_CONSUMPTION
    ):
        if webhook_queue is None:
            raise RuntimeError("WhatsApp queue is unavailable while paused")
        webhook_queue.enqueue_in(
            timedelta(seconds=10),
            process_webhook_event,
            envelope,
            job_timeout=WHATSAPP_RQ_JOB_TIMEOUT,
            retry=Retry(
                max=WHATSAPP_RQ_MAX_RETRIES,
                interval=list(WHATSAPP_RQ_RETRY_INTERVALS),
            ),
            meta={"whatsapp_initial_retries": WHATSAPP_RQ_MAX_RETRIES},
        )
        return


    retry_attempt = _retry_attempt(envelope)
    _mark_state(
        tenant_id,
        kind,
        event_id,
        "processing",
        increment_attempt=True,
        retry_attempt=retry_attempt,
    )
    try:
        from app.services import jobs

        if kind == "message":
            jobs.process_webhook_message(
                phone_number_id, payload, current_client_id=tenant_id,
                inbound_event_id=event_id, correlation_id=envelope.get("correlation_id"),
            )
        else:
            jobs.process_status_update(
                payload, current_client_id=tenant_id, phone_number_id=phone_number_id,
                require_known_intent=True,
            )
    except Exception as exc:
        if _is_retryable_error(exc):
            _mark_retry_or_dead_letter(tenant_id, kind, event_id, exc)
            raise
        _mark_state(tenant_id, kind, event_id, "dead_letter", error=exc, dead_letter=True)
        raise PermanentWebhookError(f"Permanent WhatsApp worker failure: {type(exc).__name__}") from exc
    else:
        _mark_state(tenant_id, kind, event_id, "processed")


def replay_dead_letter(*, receipt_id: int) -> str:
    """Explicit operator tool: put one dead-letter event back on the durable queue."""
    if database.SessionLocal is None:
        raise RuntimeError("WhatsApp queue requires the durable database receipt store")
    with database.SessionLocal() as session:
        receipt = session.get(WhatsAppWebhookEvent, receipt_id)
        if receipt is None or receipt.state not in {"dead_letter", "enqueue_failed"}:
            raise ValueError("Only dead-lettered or enqueue-failed WhatsApp events can be replayed")
        kind, payload, phone_number_id, client_id = (
            receipt.event_kind,
            dict(receipt.payload),
            receipt.phone_number_id,
            receipt.client_id,
        )
    return enqueue_event(kind=kind, payload=payload, phone_number_id=phone_number_id, client_id=client_id)


def _mark_retry_or_dead_letter(client_id: int, kind: str, event_id: str, error: Exception) -> None:
    job = get_current_job()
    retries_left = getattr(job, "retries_left", 0) if job else 0
    _mark_state(
        client_id,
        kind,
        event_id,
        "dead_letter" if not retries_left else "queued",
        error=error,
        dead_letter=not retries_left,
    )


def _retry_attempt(envelope: dict[str, Any]) -> int:
    """Return the RQ retry ordinal, persisted independently of job args."""
    job = get_current_job()
    if job is None:
        return int(envelope.get("attempt", 0) or 0)
    initial_retries = getattr(job, "meta", {}).get(
        "whatsapp_initial_retries", WHATSAPP_RQ_MAX_RETRIES
    )
    retries_left = getattr(job, "retries_left", initial_retries)
    return max(0, int(initial_retries) - int(retries_left))


def _is_retryable_error(error: Exception) -> bool:
    """Retry network failures and transient provider responses only."""
    if isinstance(error, _RETRYABLE_EXCEPTIONS):
        return True
    if not isinstance(error, RequestException):
        return False
    response = getattr(error, "response", None)
    if response is None:
        return True
    status_code = getattr(response, "status_code", None)
    return status_code == 408 or status_code == 429 or (isinstance(status_code, int) and status_code >= 500)


def _mark_state(
    client_id: int,
    kind: str,
    event_id: str,
    state: str,
    *,
    error: Exception | None = None,
    increment_attempt: bool = False,
    dead_letter: bool = False,
    retry_attempt: int | None = None,
) -> None:
    if database.SessionLocal is None:
        return
    with database.SessionLocal() as session:
        receipt = (
            session.query(WhatsAppWebhookEvent)
            .filter_by(client_id=client_id, event_kind=kind, event_id=event_id)
            .one_or_none()
        )
        if receipt is None:
            logger.error("WhatsApp worker receipt missing for event=%s", event_id)
            return
        receipt.state = state
        if increment_attempt:
            # attempt_count is durable execution count: first execution is 1,
            # first RQ retry is 2, and so on. Never trust a stale job arg.
            receipt.attempt_count = max(receipt.attempt_count, (retry_attempt or 0) + 1)
        if error:
            receipt.last_error = f"{type(error).__name__}: {error}"[:2000]
        if state == "processed":
            receipt.processed_at = datetime.utcnow()
        if dead_letter:
            receipt.dead_lettered_at = datetime.utcnow()
        session.commit()

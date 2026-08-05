"""Phase 12B logging context and tenant-safe metrics aggregation."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from rq import Worker
from sqlalchemy import case, func

from app.core import database
from app.core.models import (
    WhatsAppAIDecisionAudit,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppPolicyDecision,
    WhatsAppTakeoverTask,
    WhatsAppWebhookEvent,
)
from app.core.whatsapp_observability import (
    ALERT_RULES,
    MAX_CONTROL_STATES,
    evaluate_alerts,
    redact_log_value,
    safe_event_name,
)
from app.services import whatsapp_operations

_correlation_id: ContextVar[str | None] = ContextVar("whatsapp_correlation_id", default=None)
_tenant_id: ContextVar[int | None] = ContextVar("whatsapp_tenant_id", default=None)


def current_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_context(correlation_id: str, *, tenant_id: int | None) -> Iterator[None]:
    normalized = correlation_id.strip()
    if not normalized:
        raise ValueError("WhatsApp correlation ID is required")
    correlation_token = _correlation_id.set(normalized)
    tenant_token = _tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id.reset(tenant_token)
        _correlation_id.reset(correlation_token)


class RedactingJsonFormatter(logging.Formatter):
    """Emit allowlisted structured fields; all unknown fields fail closed."""

    _STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"asctime", "message"}

    def format(self, record: logging.LogRecord) -> str:
        explicit_event = getattr(record, "event", None)
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "component": safe_event_name(record.name),
            "event": safe_event_name(explicit_event if explicit_event is not None else record.msg),
            "correlation_id": getattr(record, "correlation_id", None) or current_correlation_id(),
            "tenant_id": getattr(record, "tenant_id", None) or _tenant_id.get(),
        }
        for key, value in record.__dict__.items():
            if key not in self._STANDARD and key not in payload and not key.startswith("_"):
                payload[key] = redact_log_value(value, key=key)
        if record.exc_info and record.exc_info[0] is not None:
            payload["error_type"] = safe_event_name(record.exc_info[0].__name__)
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_whatsapp_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    for name in (
        "app.api.routers.whatsapp",
        "app.api.routers.whatsapp_operations",
        "app.api.routers.whatsapp_dead_letters",
        "app.api.routers.whatsapp_observability",
        "app.clients.whatsapp_client",
        "app.services.whatsapp_inbox",
        "app.services.whatsapp_alert_delivery",
        "app.services.whatsapp_dead_letters",
        "app.api.routers.whatsapp_observability",
        "app.clients.whatsapp_client",
        "app.services.whatsapp_inbox",
        "app.services.whatsapp_operations",
        "app.services.whatsapp_outbox",
        "app.services.whatsapp_policy",
        "app.services.whatsapp_queue",
        "app.services.whatsapp_sequences",
        "app.services.ai_decision",
        "app.services.jobs",
    ):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)


class ProcessMetrics:
    """Bounded, explicitly process-local ingress measurements."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._acks: dict[int | None, dict[str, float]] = {}
        self._duplicates: dict[int, int] = {}

    def observe_webhook_ack(self, latency_ms: float, *, client_ids: set[int] | None = None) -> None:
        with self._lock:
            scopes: set[int | None] = {None}
            scopes.update(client_ids or set())
            for scope in scopes:
                values = self._acks.setdefault(scope, {"count": 0.0, "total": 0.0, "maximum": 0.0, "latest": 0.0})
                values["count"] += 1
                values["total"] += latency_ms
                values["maximum"] = max(values["maximum"], latency_ms)
                values["latest"] = latency_ms

    def increment_duplicate(self, client_id: int) -> None:
        with self._lock:
            self._duplicates[client_id] = self._duplicates.get(client_id, 0) + 1

    def snapshot(self, *, client_id: int | None) -> dict[str, Any]:
        with self._lock:
            values = self._acks.get(client_id)
            duplicates = sum(self._duplicates.values()) if client_id is None else self._duplicates.get(client_id, 0)
            return {
                "webhook_ack_latency_ms": {
                    "count": int(values["count"]) if values else 0,
                    "average": values["total"] / values["count"] if values else None,
                    "maximum": values["maximum"] if values else None,
                    "latest": values["latest"] if values else None,
                    "scope": "current_api_process",
                },
                "duplicate_events_process_total": {"value": duplicates, "scope": "current_api_process"},
            }


process_metrics = ProcessMetrics()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _seconds_since(value: datetime | None, now: datetime) -> float | None:
    normalized = _utc(value)
    return None if normalized is None else max(0.0, (now - normalized).total_seconds())


def _database_metrics(*, client_id: int | None, now: datetime) -> dict[str, Any]:
    if database.SessionLocal is None:
        raise RuntimeError("database session unavailable")
    since = now - timedelta(minutes=15)
    with database.SessionLocal() as session:
        receipts = session.query(WhatsAppWebhookEvent)
        intents = session.query(WhatsAppOutboundIntent)
        policies = session.query(WhatsAppPolicyDecision)
        ai_audits = session.query(WhatsAppAIDecisionAudit)
        opt_outs = session.query(WhatsAppOptOut)
        takeover = session.query(WhatsAppTakeoverTask)
        if client_id is not None:
            receipts = receipts.filter(WhatsAppWebhookEvent.client_id == client_id)
            intents = intents.filter(WhatsAppOutboundIntent.client_id == client_id)
            policies = policies.filter(WhatsAppPolicyDecision.client_id == client_id)
            ai_audits = ai_audits.filter(WhatsAppAIDecisionAudit.client_id == client_id)
            opt_outs = opt_outs.filter(WhatsAppOptOut.client_id == client_id)
            takeover = takeover.filter(WhatsAppTakeoverTask.client_id == client_id)

        retries = receipts.with_entities(func.coalesce(func.sum(case((WhatsAppWebhookEvent.attempt_count > 1, WhatsAppWebhookEvent.attempt_count - 1), else_=0)), 0)).scalar()
        dead_letters = receipts.filter(WhatsAppWebhookEvent.state == "dead_letter").count()
        enqueue_failures = receipts.filter(WhatsAppWebhookEvent.state == "enqueue_failed").count()
        provider_status_failures = intents.filter(WhatsAppOutboundIntent.provider_status == "failed").count()
        provider_send_failures = intents.filter(WhatsAppOutboundIntent.created_at >= since, WhatsAppOutboundIntent.state.in_(("failed", "unknown"))).count()
        policy_blocks = policies.filter(WhatsAppPolicyDecision.created_at >= since, WhatsAppPolicyDecision.decision == "blocked").count()
        total_ai = ai_audits.filter(WhatsAppAIDecisionAudit.created_at >= since).count()
        escalated_ai = ai_audits.filter(WhatsAppAIDecisionAudit.created_at >= since, WhatsAppAIDecisionAudit.decision == "ESCALATE").count()
        oldest_takeover = takeover.filter(WhatsAppTakeoverTask.status.in_(("open", "acknowledged"))).with_entities(func.min(WhatsAppTakeoverTask.created_at)).scalar()

        duplicate_query = session.query(WhatsAppOutboundIntent.client_id, WhatsAppOutboundIntent.provider_message_id).filter(WhatsAppOutboundIntent.provider_message_id.is_not(None))
        if client_id is not None:
            duplicate_query = duplicate_query.filter(WhatsAppOutboundIntent.client_id == client_id)
        duplicate_subquery = duplicate_query.group_by(WhatsAppOutboundIntent.client_id, WhatsAppOutboundIntent.provider_message_id).having(func.count(WhatsAppOutboundIntent.id) > 1).subquery()
        duplicate_breaches = session.query(func.count()).select_from(duplicate_subquery).scalar()
        return {
            "retries_total": int(retries or 0),
            "dead_letter_count": dead_letters,
            "enqueue_failed_count": enqueue_failures,
            "provider_send_failures_15m": provider_send_failures,
            "provider_status_failures_total": provider_status_failures,
            "policy_blocks_15m": policy_blocks,
            "ai_escalations_15m": escalated_ai,
            "ai_decisions_15m": total_ai,
            "ai_escalation_rate_15m": escalated_ai / total_ai if total_ai else 0.0,
            "opt_outs_total": opt_outs.count(),
            "oldest_takeover_queue_age_seconds": _seconds_since(oldest_takeover, now),
            "duplicate_send_invariant_breaches": int(duplicate_breaches or 0),
        }


def _infrastructure_metrics(*, now: datetime) -> dict[str, Any]:
    from app.api.runtime import redis_conn, webhook_queue

    result: dict[str, Any] = {"redis_available": False, "queue_depth": None, "oldest_queue_age_seconds": None, "worker_count": 0, "worker_heartbeat_age_seconds": None}
    if redis_conn is None or webhook_queue is None:
        return result
    try:
        redis_conn.ping()
        result["redis_available"] = True
        result["queue_depth"] = webhook_queue.count
        jobs = webhook_queue.get_jobs(offset=0, length=1)
        if jobs:
            result["oldest_queue_age_seconds"] = _seconds_since(getattr(jobs[0], "enqueued_at", None), now)
        workers = [worker for worker in Worker.all(connection=redis_conn) if webhook_queue.name in worker.queue_names()]
        result["worker_count"] = len(workers)
        heartbeats = [_utc(getattr(worker, "last_heartbeat", None)) for worker in workers]
        valid = [heartbeat for heartbeat in heartbeats if heartbeat is not None]
        if valid:
            result["worker_heartbeat_age_seconds"] = _seconds_since(max(valid), now)
    except Exception:
        result["redis_available"] = False
    return result


def collect_metrics(*, client_id: int | None, include_infrastructure: bool) -> dict[str, Any]:
    """Return bounded content-minimised metrics within the requested tenant scope."""
    now = datetime.now(timezone.utc)
    metrics = process_metrics.snapshot(client_id=client_id)
    metrics["database_available"] = True
    try:
        metrics.update(_database_metrics(client_id=client_id, now=now))
        controls = whatsapp_operations.list_states(client_id=client_id)
        if client_id is not None:
            controls = whatsapp_operations.list_states(client_id=None) + controls
        metrics["kill_switches_truncated"] = len(controls) > MAX_CONTROL_STATES
        metrics["kill_switches"] = [
            {"control": state["control"], "scope": state["scope"], "resource_id": state["resource_id"], "effective_enabled": state["effective_enabled"], "version": state["version"]}
            for state in controls[:MAX_CONTROL_STATES]
        ]
        metrics["kill_switch_active"] = any(not state["effective_enabled"] for state in controls)
    except Exception:
        metrics["database_available"] = False
        metrics["kill_switches"] = []
        metrics["kill_switches_truncated"] = False
        metrics["kill_switch_active"] = True
    if include_infrastructure:
        metrics.update(_infrastructure_metrics(now=now))
    return {"scope": "global" if client_id is None else "tenant", "client_id": client_id, "generated_at": now, "metrics": metrics}


def alert_rules_payload() -> dict[str, Any]:
    return {"rules": list(ALERT_RULES)}


def active_alerts_payload() -> dict[str, Any]:
    snapshot = collect_metrics(client_id=None, include_infrastructure=True)
    return {"generated_at": snapshot["generated_at"], "alerts": evaluate_alerts(snapshot["metrics"])}

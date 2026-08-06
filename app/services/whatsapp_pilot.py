"""Fail-closed Phase 13 controls for one consented WhatsApp pilot tenant."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rq import Worker
from sqlalchemy import func

from app.core import config, database
from app.core.models import (
    Client,
    Lead,
    WhatsAppAIDecisionAudit,
    WhatsAppConsentRecord,
    WhatsAppOperationalControlAudit,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppPolicyDecision,
    WhatsAppSequence,
    WhatsAppSequenceStep,
    WhatsAppTakeoverTask,
    WhatsAppTemplate,
    WhatsAppWebhookEvent,
)
from app.services import whatsapp_operations

STAGE_INBOUND_OBSERVATION = 1
STAGE_AI_REPLIES = 2
STAGE_OUTBOUND_SEQUENCE = 3
_MAX_COHORT_SIZE = 100
_HASH = re.compile(r"^[0-9a-f]{64}$")


class PilotError(RuntimeError):
    """Pilot configuration or runtime evidence is incomplete."""


class PilotConflict(PilotError):
    """Pilot transition does not match the durable current state."""


@dataclass(frozen=True)
class PilotConfig:
    tenant_id: int
    approval_reference: str
    approval_expires_at: datetime
    cohort_hashes: frozenset[str]
    approved_template_ids: frozenset[int]
    sequence_id: int
    timezone_name: str
    operating_start: time
    operating_end: time
    daily_cap: int
    total_cap: int
    success_min_delivery_rate: float
    success_min_reply_rate: float
    warning_provider_failures: int
    warning_queue_age_seconds: int
    stop_provider_failures: int
    stop_dead_letters: int
    stop_ai_escalations: int
    stop_queue_age_seconds: int
    max_worker_heartbeat_age_seconds: int


@dataclass(frozen=True)
class PilotReadiness:
    ready: bool
    reasons: tuple[str, ...]
    config: PilotConfig | None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise PilotError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise PilotError(f"{name} must be a positive integer")
    return value


def _rate(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PilotError(f"{name} must be between 0 and 1") from exc
    if not 0 <= value <= 1:
        raise PilotError(f"{name} must be between 0 and 1")
    return value


def _csv_ints(raw: str, name: str) -> frozenset[int]:
    values = frozenset(
        _positive_int(item.strip(), name)
        for item in raw.split(",")
        if item.strip()
    )
    if not values:
        raise PilotError(f"{name} is required")
    return values


def load_config() -> PilotConfig:
    """Validate the complete static pilot bundle; partial config is invalid."""
    if not config.WHATSAPP_PILOT_CONFIG_ENABLED:
        raise PilotError("pilot_config_disabled")
    if not config.WHATSAPP_PILOT_APPROVAL_REFERENCE:
        raise PilotError("pilot approval reference is required")
    try:
        expires = datetime.fromisoformat(
            config.WHATSAPP_PILOT_APPROVAL_EXPIRES_AT.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise PilotError("pilot approval expiry must be RFC3339") from exc
    if expires.tzinfo is None:
        raise PilotError("pilot approval expiry must include a timezone")
    hashes = frozenset(
        item.strip().lower()
        for item in config.WHATSAPP_PILOT_COHORT_HASHES.split(",")
        if item.strip()
    )
    if (
        not hashes
        or len(hashes) > _MAX_COHORT_SIZE
        or any(not _HASH.fullmatch(item) for item in hashes)
    ):
        raise PilotError("pilot cohort hashes must contain 1 to 100 SHA-256 digests")
    try:
        ZoneInfo(config.WHATSAPP_PILOT_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise PilotError("pilot timezone is invalid") from exc
    try:
        operating_start = time.fromisoformat(config.WHATSAPP_PILOT_OPERATING_START)
        operating_end = time.fromisoformat(config.WHATSAPP_PILOT_OPERATING_END)
    except ValueError as exc:
        raise PilotError("pilot operating hours must use HH:MM") from exc
    if operating_start == operating_end:
        raise PilotError("pilot operating window cannot be empty")
    daily_cap = _positive_int(config.WHATSAPP_PILOT_DAILY_CAP, "pilot daily cap")
    total_cap = _positive_int(config.WHATSAPP_PILOT_TOTAL_CAP, "pilot total cap")
    if daily_cap > total_cap:
        raise PilotError("pilot daily cap cannot exceed total cap")
    return PilotConfig(
        tenant_id=_positive_int(config.WHATSAPP_PILOT_TENANT_ID, "pilot tenant ID"),
        approval_reference=config.WHATSAPP_PILOT_APPROVAL_REFERENCE,
        approval_expires_at=_utc(expires),
        cohort_hashes=hashes,
        approved_template_ids=_csv_ints(
            config.WHATSAPP_PILOT_APPROVED_TEMPLATE_IDS, "pilot template IDs"
        ),
        sequence_id=_positive_int(config.WHATSAPP_PILOT_SEQUENCE_ID, "pilot sequence ID"),
        timezone_name=config.WHATSAPP_PILOT_TIMEZONE,
        operating_start=operating_start,
        operating_end=operating_end,
        daily_cap=daily_cap,
        total_cap=total_cap,
        success_min_delivery_rate=_rate(
            config.WHATSAPP_PILOT_SUCCESS_MIN_DELIVERY_RATE,
            "pilot minimum delivery rate",
        ),
        success_min_reply_rate=_rate(
            config.WHATSAPP_PILOT_SUCCESS_MIN_REPLY_RATE,
            "pilot minimum reply rate",
        ),
        warning_provider_failures=_positive_int(
            config.WHATSAPP_PILOT_WARNING_PROVIDER_FAILURES,
            "pilot provider-failure warning threshold",
        ),
        warning_queue_age_seconds=_positive_int(
            config.WHATSAPP_PILOT_WARNING_QUEUE_AGE_SECONDS,
            "pilot queue-age warning threshold",
        ),
        stop_provider_failures=_positive_int(
            config.WHATSAPP_PILOT_STOP_PROVIDER_FAILURES,
            "pilot provider-failure stop threshold",
        ),
        stop_dead_letters=_positive_int(
            config.WHATSAPP_PILOT_STOP_DEAD_LETTERS,
            "pilot dead-letter stop threshold",
        ),
        stop_ai_escalations=_positive_int(
            config.WHATSAPP_PILOT_STOP_AI_ESCALATIONS,
            "pilot AI-escalation stop threshold",
        ),
        stop_queue_age_seconds=_positive_int(
            config.WHATSAPP_PILOT_STOP_QUEUE_AGE_SECONDS,
            "pilot queue-age stop threshold",
        ),
        max_worker_heartbeat_age_seconds=_positive_int(
            config.WHATSAPP_PILOT_MAX_WORKER_HEARTBEAT_AGE_SECONDS,
            "pilot worker-heartbeat threshold",
        ),
    )


def cohort_digest(phone: str) -> str:
    normalized = re.sub(r"\D", "", phone or "")
    if not normalized:
        raise PilotError("pilot recipient phone is invalid")
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def _current_stage_locked(session, client_id: int, *, lock: bool = False) -> int:
    stage_2 = whatsapp_operations.state_locked(
        session,
        whatsapp_operations.PILOT_STAGE_2,
        client_id=client_id,
        lock=lock,
    ).enabled
    stage_3 = whatsapp_operations.state_locked(
        session,
        whatsapp_operations.PILOT_STAGE_3,
        client_id=client_id,
        lock=lock,
    ).enabled
    if stage_3 and not stage_2:
        raise PilotError("pilot stage controls are inconsistent")
    if stage_3:
        return STAGE_OUTBOUND_SEQUENCE
    return STAGE_AI_REPLIES if stage_2 else STAGE_INBOUND_OBSERVATION


def _template_ready(row: WhatsAppTemplate, cfg: PilotConfig, now: datetime) -> bool:
    return bool(
        row.client_id == cfg.tenant_id
        and row.id in cfg.approved_template_ids
        and row.approval_status == "approved"
        and row.meta_status == "approved"
        and row.retired_at is None
        and row.verification_reference
        and row.verified_at
        and row.verification_expires_at
        and _utc(row.verification_expires_at) > now
    )


def readiness_locked(
    session, *, client_id: int, now: datetime | None = None
) -> PilotReadiness:
    current = _utc(now or datetime.now(timezone.utc))
    try:
        cfg = load_config()
    except PilotError as exc:
        return PilotReadiness(False, (str(exc),), None)
    reasons: list[str] = []
    if client_id != cfg.tenant_id:
        reasons.append("tenant_not_approved")
    if cfg.approval_expires_at <= current:
        reasons.append("pilot_approval_stale")
    if session.query(Client).filter_by(id=cfg.tenant_id, is_active=True).one_or_none() is None:
        reasons.append("approved_tenant_unavailable")

    consents = session.query(WhatsAppConsentRecord).filter_by(
        client_id=cfg.tenant_id, revoked_at=None
    ).all()
    consent_by_hash = {cohort_digest(row.phone): row for row in consents}
    for digest in cfg.cohort_hashes:
        consent = consent_by_hash.get(digest)
        if consent is None:
            reasons.append("cohort_consent_missing")
            break
        if (
            _utc(consent.consented_at) > current
            or not consent.source.strip()
            or not (consent.evidence_reference or "").strip()
            or not consent.policy_version.strip()
        ):
            reasons.append("cohort_consent_evidence_invalid")
            break
        if session.query(WhatsAppOptOut.id).filter_by(
            client_id=cfg.tenant_id, phone=consent.phone
        ).first() is not None:
            reasons.append("cohort_contains_opt_out")
            break
        if session.query(Lead.id).filter_by(
            client_id=cfg.tenant_id, phone=consent.phone
        ).first() is None:
            reasons.append("cohort_recipient_not_tenant_lead")
            break

    templates = session.query(WhatsAppTemplate).filter(
        WhatsAppTemplate.id.in_(cfg.approved_template_ids)
    ).all()
    if len(templates) != len(cfg.approved_template_ids) or any(
        not _template_ready(row, cfg, current) for row in templates
    ):
        reasons.append("pilot_template_evidence_missing_or_stale")

    sequence = session.query(WhatsAppSequence).filter_by(
        id=cfg.sequence_id, client_id=cfg.tenant_id
    ).one_or_none()
    if sequence is None or sequence.status not in {"active", "paused"}:
        reasons.append("pilot_sequence_unavailable")
    else:
        step_template_ids = {
            value
            for (value,) in session.query(WhatsAppSequenceStep.template_id)
            .filter_by(sequence_id=sequence.id)
            .all()
        }
        if not step_template_ids or not step_template_ids.issubset(
            cfg.approved_template_ids
        ):
            reasons.append("pilot_sequence_uses_unapproved_template")
    return PilotReadiness(not reasons, tuple(dict.fromkeys(reasons)), cfg)


def _pilot_started_at_locked(session, client_id: int) -> datetime | None:
    value = session.query(func.max(WhatsAppOperationalControlAudit.created_at)).filter(
        WhatsAppOperationalControlAudit.client_id == client_id,
        WhatsAppOperationalControlAudit.control_type == whatsapp_operations.PILOT_ENABLED,
        WhatsAppOperationalControlAudit.to_enabled.is_(True),
    ).scalar()
    return _utc(value) if value else None


def _within_operating_hours(cfg: PilotConfig, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(cfg.timezone_name)).time().replace(tzinfo=None)
    if cfg.operating_start < cfg.operating_end:
        return cfg.operating_start <= local < cfg.operating_end
    return local >= cfg.operating_start or local < cfg.operating_end


def _accepted_counts_locked(
    session, cfg: PilotConfig, now: datetime
) -> tuple[int, int]:
    local = now.astimezone(ZoneInfo(cfg.timezone_name))
    midnight = datetime.combine(
        local.date(), time.min, tzinfo=local.tzinfo
    ).astimezone(timezone.utc)
    rows = session.query(
        WhatsAppPolicyDecision.phone, WhatsAppPolicyDecision.created_at
    ).filter(
        WhatsAppPolicyDecision.client_id == cfg.tenant_id,
        WhatsAppPolicyDecision.provider_outcome == "accepted",
        WhatsAppPolicyDecision.action.endswith("_send"),
    ).all()
    cohort_rows = [row for row in rows if cohort_digest(row.phone) in cfg.cohort_hashes]
    return sum(_utc(row.created_at) >= midnight for row in cohort_rows), len(cohort_rows)


def runtime_health() -> dict[str, Any]:
    """Return content-free queue/worker evidence; failures remain explicit."""
    from app.api.runtime import redis_conn, webhook_queue

    result: dict[str, Any] = {
        "redis_available": False,
        "queue_depth": None,
        "oldest_queue_age_seconds": None,
        "worker_count": 0,
        "worker_heartbeat_age_seconds": None,
    }
    if redis_conn is None or webhook_queue is None:
        return result
    try:
        redis_conn.ping()
        result["redis_available"] = True
        result["queue_depth"] = webhook_queue.count
        jobs = webhook_queue.get_jobs(offset=0, length=1)
        now = datetime.now(timezone.utc)
        if jobs and jobs[0].enqueued_at:
            result["oldest_queue_age_seconds"] = max(
                0.0, (now - _utc(jobs[0].enqueued_at)).total_seconds()
            )
        workers = [
            worker
            for worker in Worker.all(connection=redis_conn)
            if webhook_queue.name in worker.queue_names()
        ]
        result["worker_count"] = len(workers)
        heartbeats = [
            _utc(worker.last_heartbeat)
            for worker in workers
            if worker.last_heartbeat is not None
        ]
        if heartbeats:
            result["worker_heartbeat_age_seconds"] = max(
                0.0, (now - max(heartbeats)).total_seconds()
            )
    except Exception:
        result["redis_available"] = False
    return result


def _thresholds_locked(
    session,
    cfg: PilotConfig,
    since: datetime | None,
    infrastructure: dict[str, Any],
) -> dict[str, Any]:
    start = since or datetime.now(timezone.utc)
    intents = session.query(WhatsAppOutboundIntent).filter(
        WhatsAppOutboundIntent.client_id == cfg.tenant_id,
        WhatsAppOutboundIntent.created_at >= start,
    )
    provider_failures = intents.filter(
        WhatsAppOutboundIntent.state.in_(("failed", "unknown"))
    ).count()
    dead_letters = session.query(WhatsAppWebhookEvent).filter(
        WhatsAppWebhookEvent.client_id == cfg.tenant_id,
        WhatsAppWebhookEvent.state == "dead_letter",
        WhatsAppWebhookEvent.received_at >= start,
    ).count()
    escalations = session.query(WhatsAppAIDecisionAudit).filter(
        WhatsAppAIDecisionAudit.client_id == cfg.tenant_id,
        WhatsAppAIDecisionAudit.decision == "ESCALATE",
        WhatsAppAIDecisionAudit.created_at >= start,
    ).count()
    duplicate_breaches = session.query(
        WhatsAppOutboundIntent.provider_message_id
    ).filter(
        WhatsAppOutboundIntent.client_id == cfg.tenant_id,
        WhatsAppOutboundIntent.provider_message_id.is_not(None),
        WhatsAppOutboundIntent.created_at >= start,
    ).group_by(WhatsAppOutboundIntent.provider_message_id).having(
        func.count(WhatsAppOutboundIntent.id) > 1
    ).count()
    queue_age = infrastructure.get("oldest_queue_age_seconds")
    heartbeat_age = infrastructure.get("worker_heartbeat_age_seconds")
    infrastructure_stop = bool(
        infrastructure.get("redis_available") is not True
        or not infrastructure.get("worker_count")
        or heartbeat_age is None
        or heartbeat_age > cfg.max_worker_heartbeat_age_seconds
        or (queue_age is not None and queue_age >= cfg.stop_queue_age_seconds)
    )
    return {
        "provider_failures": provider_failures,
        "dead_letters": dead_letters,
        "ai_escalations": escalations,
        "duplicate_send_breaches": duplicate_breaches,
        "infrastructure_stop": infrastructure_stop,
        "warning": bool(
            provider_failures >= cfg.warning_provider_failures
            or (queue_age is not None and queue_age >= cfg.warning_queue_age_seconds)
        ),
        "triggered": bool(
            duplicate_breaches
            or provider_failures >= cfg.stop_provider_failures
            or dead_letters >= cfg.stop_dead_letters
            or escalations >= cfg.stop_ai_escalations
            or infrastructure_stop
        ),
    }


def final_send_gate_locked(
    session,
    *,
    client: Client,
    lead: Lead | None,
    action: str,
    message_type: str,
    template: WhatsAppTemplate | None = None,
    recipient_kind: str = "lead",
    sequence_id: int | None = None,
    now: datetime | None = None,
) -> str | None:
    """Recheck Phase 13 evidence in the final provider transaction.

    The gate is ONLY active when the pilot is live (config enabled) and the
    client is the approved pilot tenant.  All other traffic passes through
    immediately so that pre-pilot behaviour is fully preserved.
    """
    # Fast-pass 1: operator messages are never pilot-gated.
    if recipient_kind == "operator":
        return None
    # Fast-pass 2: pilot must be configured.
    if not config.WHATSAPP_PILOT_CONFIG_ENABLED:
        return None
    # Fast-pass 3: gate only applies to the approved pilot tenant.
    try:
        pilot_tenant_id = int(config.WHATSAPP_PILOT_TENANT_ID or "")
    except (TypeError, ValueError):
        return "pilot_prerequisite_missing_or_stale"
    if client.id != pilot_tenant_id:
        return None

    current = _utc(now or datetime.now(timezone.utc))
    readiness = readiness_locked(session, client_id=client.id, now=current)
    if not readiness.ready or readiness.config is None:
        return "pilot_prerequisite_missing_or_stale"
    cfg = readiness.config
    if not whatsapp_operations.state_locked(
        session,
        whatsapp_operations.PILOT_ENABLED,
        client_id=client.id,
        lock=True,
    ).enabled:
        return "pilot_stopped"
    try:
        stage = _current_stage_locked(session, client.id, lock=True)
    except PilotError:
        return "pilot_stage_invalid"
    if lead is None or cohort_digest(lead.phone) not in cfg.cohort_hashes:
        return "pilot_cohort_denied"
    if action == "sequence_step_send":
        if stage != STAGE_OUTBOUND_SEQUENCE or sequence_id != cfg.sequence_id:
            return "pilot_stage_denied"
        if template is None or template.id not in cfg.approved_template_ids:
            return "pilot_template_denied"
    elif action in {"queued_reply_send", "human_manual_send"}:
        if stage < STAGE_AI_REPLIES or message_type != "text":
            return "pilot_stage_denied"
    else:
        return "pilot_action_denied"
    if not _within_operating_hours(cfg, current):
        return "pilot_operating_hours"
    daily_count, total_count = _accepted_counts_locked(session, cfg, current)
    if daily_count >= cfg.daily_cap:
        return "pilot_daily_cap"
    if total_count >= cfg.total_cap:
        return "pilot_total_cap"
    thresholds = _thresholds_locked(
        session,
        cfg,
        _pilot_started_at_locked(session, client.id),
        runtime_health(),
    )
    if thresholds["triggered"]:
        return "pilot_stop_threshold"
    return None


def _state_version(control: str, client_id: int) -> int:
    if database.SessionLocal is None:
        raise PilotError("pilot controls require the durable database")
    with database.SessionLocal() as session:
        return whatsapp_operations.state_locked(
            session, control, client_id=client_id, lock=True
        ).version


def transition_stage(
    *,
    client_id: int,
    expected_stage: int,
    target_stage: int,
    expected_version_stage_2: int,
    expected_version_stage_3: int,
    operator_id: str,
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    if database.SessionLocal is None:
        raise PilotError("pilot controls require the durable database")
    if target_stage not in {1, 2, 3} or abs(target_stage - expected_stage) != 1:
        raise PilotConflict("pilot stages must move exactly one stage at a time")
    with database.SessionLocal() as session:
        current = _current_stage_locked(session, client_id)
        enabled = whatsapp_operations.state_locked(
            session,
            whatsapp_operations.PILOT_ENABLED,
            client_id=client_id,
            lock=False,
        ).enabled
        readiness = readiness_locked(session, client_id=client_id)
    if current != expected_stage:
        raise PilotConflict(
            f"stale pilot stage: expected {expected_stage}, current {current}"
        )
    if target_stage > current and (not enabled or not readiness.ready):
        raise PilotConflict("pilot must be enabled and ready before stage expansion")

    requests = [
        whatsapp_operations.MutationRequest(
            control=whatsapp_operations.PILOT_STAGE_2,
            enabled_value=target_stage >= 2,
            expected_version=expected_version_stage_2,
        ),
        whatsapp_operations.MutationRequest(
            control=whatsapp_operations.PILOT_STAGE_3,
            enabled_value=target_stage >= 3,
            expected_version=expected_version_stage_3,
        )
    ]
    try:
        states = whatsapp_operations.mutate_multiple(
            requests=requests,
            operator_id=operator_id,
            reason=reason,
            correlation_id=correlation_id,
            client_id=client_id,
        )
    except whatsapp_operations.OperationalControlConflict as e:
        raise PilotConflict(str(e)) from e

    s2 = states[whatsapp_operations.PILOT_STAGE_2].enabled
    s3 = states[whatsapp_operations.PILOT_STAGE_3].enabled
    if s3 and not s2:
        raise PilotError("pilot stage corruption: stage 3 enabled without stage 2")
    
    return {
        "stage": target_stage,
        "controls": {
            k: v.as_dict() for k, v in states.items()
        }
    }


def set_enabled(
    *,
    client_id: int,
    enabled: bool,
    expected_version: int,
    operator_id: str,
    reason: str,
    correlation_id: str,
) -> dict[str, Any]:
    if database.SessionLocal is None:
        raise PilotError("pilot controls require the durable database")
    with database.SessionLocal() as session:
        stage = _current_stage_locked(session, client_id, lock=True)
        readiness = readiness_locked(session, client_id=client_id)
        if enabled:
            if stage != STAGE_INBOUND_OBSERVATION:
                raise PilotConflict("safe resume requires an explicit return to stage 1")
            if not readiness.ready or readiness.config is None:
                raise PilotConflict("pilot prerequisites are incomplete or stale")
            thresholds = _thresholds_locked(
                session,
                readiness.config,
                _pilot_started_at_locked(session, client_id),
                runtime_health(),
            )
            if thresholds["triggered"]:
                raise PilotConflict("pilot stop thresholds must be cleared before resume")
    return whatsapp_operations.mutate(
        control=whatsapp_operations.PILOT_ENABLED,
        enabled_value=enabled,
        expected_version=expected_version,
        operator_id=operator_id,
        reason=reason,
        correlation_id=correlation_id,
        client_id=client_id,
    ).as_dict()


def status(*, client_id: int) -> dict[str, Any]:
    """Return protected, tenant-scoped, content-minimised pilot evidence."""
    if database.SessionLocal is None:
        raise PilotError("pilot visibility requires the durable database")
    now = datetime.now(timezone.utc)
    with database.SessionLocal() as session:
        readiness = readiness_locked(session, client_id=client_id, now=now)
        enabled_state = whatsapp_operations.state_locked(
            session, whatsapp_operations.PILOT_ENABLED, client_id=client_id
        )
        try:
            stage = _current_stage_locked(session, client_id)
            stage_valid = True
        except PilotError:
            stage, stage_valid = 1, False
        cfg = readiness.config
        if cfg is None or client_id != cfg.tenant_id:
            raise PilotError("tenant is not the approved pilot tenant")
        started_at = _pilot_started_at_locked(session, client_id)
        since = started_at or now
        policy_rows = session.query(WhatsAppPolicyDecision).filter(
            WhatsAppPolicyDecision.client_id == client_id,
            WhatsAppPolicyDecision.created_at >= since,
            WhatsAppPolicyDecision.action.endswith("_send"),
        ).all()
        policy_rows = [
            row for row in policy_rows if cohort_digest(row.phone) in cfg.cohort_hashes
        ]
        attempted = len(policy_rows)
        succeeded = sum(row.provider_outcome == "accepted" for row in policy_rows)
        failed = sum(
            row.provider_outcome in {"failed", "accepted_uncommitted"}
            for row in policy_rows
        )
        replies = session.query(WhatsAppWebhookEvent).filter(
            WhatsAppWebhookEvent.client_id == client_id,
            WhatsAppWebhookEvent.event_kind == "message",
            WhatsAppWebhookEvent.received_at >= since,
        ).count()
        opt_outs = session.query(WhatsAppOptOut).filter(
            WhatsAppOptOut.client_id == client_id,
            WhatsAppOptOut.opted_out_at >= since,
        ).count()
        escalations = session.query(WhatsAppAIDecisionAudit).filter(
            WhatsAppAIDecisionAudit.client_id == client_id,
            WhatsAppAIDecisionAudit.decision == "ESCALATE",
            WhatsAppAIDecisionAudit.created_at >= since,
        ).count()
        takeovers = session.query(WhatsAppTakeoverTask).filter(
            WhatsAppTakeoverTask.client_id == client_id,
            WhatsAppTakeoverTask.created_at >= since,
        ).count()
        infrastructure = runtime_health()
        thresholds = _thresholds_locked(session, cfg, started_at, infrastructure)
        last_action = session.query(WhatsAppOperationalControlAudit).filter(
            WhatsAppOperationalControlAudit.client_id == client_id,
            WhatsAppOperationalControlAudit.control_type.in_(
                tuple(whatsapp_operations.PILOT_CONTROLS)
            ),
        ).order_by(WhatsAppOperationalControlAudit.created_at.desc()).first()
        delivered = sum(
            row.provider_outcome == "accepted" for row in policy_rows
        )
        delivery_rate = delivered / attempted if attempted else 0.0
        reply_rate = replies / succeeded if succeeded else 0.0
        return {
            "generated_at": now,
            "pilot": {
                "stage": stage,
                "stage_valid": stage_valid,
                "enabled": enabled_state.enabled,
                "effective_enabled": bool(enabled_state.enabled and readiness.ready),
                "approved_tenant_id": cfg.tenant_id,
                "cohort_size": len(cfg.cohort_hashes),
                "approval_expires_at": cfg.approval_expires_at,
                "readiness": {"ready": readiness.ready, "reasons": list(readiness.reasons)},
            },
            "activity": {
                "sends_attempted": attempted,
                "sends_succeeded": succeeded,
                "sends_failed": failed,
                "replies_received": replies,
                "opt_outs": opt_outs,
                "ai_escalations": escalations,
                "human_takeovers": takeovers,
                "provider_failures": thresholds["provider_failures"],
                "dead_letters": thresholds["dead_letters"],
            },
            "success_metrics": {
                "delivery_rate": delivery_rate,
                "reply_rate": reply_rate,
                "delivery_target_met": delivery_rate >= cfg.success_min_delivery_rate,
                "reply_target_met": reply_rate >= cfg.success_min_reply_rate,
            },
            "queue_worker_health": infrastructure,
            "stop_thresholds": thresholds,
            "last_operator_action": None
            if last_action is None
            else {
                "action": last_action.control_type,
                "enabled": last_action.to_enabled,
                "operator_id": last_action.operator_id,
                "reason": last_action.reason,
                "correlation_id": last_action.correlation_id,
                "created_at": last_action.created_at,
            },
        }

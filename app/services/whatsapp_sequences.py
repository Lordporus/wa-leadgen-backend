"""Phase 8 controlled, template-only WhatsApp follow-up sequences.

All automatic delivery enters through :func:`run_sequence_tick_job`, which is
scheduled by the dedicated RQ worker.  The web process never ticks sequences.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import joinedload
from redis.exceptions import LockError

from app.core import database
from app.core.models import (
    Lead,
    Message,
    WhatsAppOptOut,
    WhatsAppSequence,
    WhatsAppSequenceEnrollment,
    WhatsAppSequenceExecution,
    WhatsAppSequenceStep,
    WhatsAppTemplate,
)
from app.services import whatsapp_outbox, whatsapp_policy

logger = logging.getLogger(__name__)
SEQUENCE_STATUSES = frozenset({"draft", "active", "paused", "archived"})
ENROLLMENT_STATUSES = frozenset({"active", "paused", "completed", "stopped"})
FAILURE_THRESHOLD = 3
_MAX_DUE_PER_TICK = 50
_RETRY_DELAY = timedelta(minutes=5)
_SCHEDULER_LOCK_KEY = "whatsapp-sequence-scheduler-lock"
_SCHEDULER_LOCK_SECONDS = 120
_SCHEDULER_RENEW_INTERVAL_SECONDS = 40
_SCHEDULER_JOB_ID = "whatsapp-sequence-tick"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _factory():
    if database.SessionLocal is None:
        raise RuntimeError("WhatsApp sequences require the durable database")
    return database.SessionLocal


def _sequence_dict(sequence: WhatsAppSequence) -> dict:
    return {
        "id": sequence.id,
        "name": sequence.name,
        "status": sequence.status,
        "created_at": sequence.created_at,
        "updated_at": sequence.updated_at,
        "steps": [
            {
                "id": s.id,
                "position": s.position,
                "delay_seconds": s.delay_seconds,
                "template_id": s.template_id,
                "parameters": s.parameters,
            }
            for s in sequence.steps
        ],
    }


def _enrollment_dict(
    enrollment: WhatsAppSequenceEnrollment, lead: Lead | None = None
) -> dict:
    return {
        "id": enrollment.id,
        "sequence_id": enrollment.sequence_id,
        "lead_id": enrollment.lead_id,
        "status": enrollment.status,
        "current_step": enrollment.current_step,
        "next_run_at": enrollment.next_run_at,
        "last_run_at": enrollment.last_run_at,
        "failure_count": enrollment.failure_count,
        "stop_reason": enrollment.stop_reason,
        "lead_name": lead.name if lead else None,
        "lead_phone": lead.phone if lead else None,
    }


def _replace_steps(
    session, sequence: WhatsAppSequence, steps: list[dict[str, Any]]
) -> None:
    if not steps:
        raise ValueError("steps are required")
    session.query(WhatsAppSequenceStep).filter_by(sequence_id=sequence.id).delete()
    for position, item in enumerate(steps):
        template_id = item.get("template_id")
        delay_seconds = int(item.get("delay_seconds", 0))
        if not isinstance(template_id, int) or delay_seconds < 0:
            raise ValueError(
                f"step {position} requires template_id and non-negative delay_seconds"
            )
        template = (
            session.query(WhatsAppTemplate)
            .filter_by(id=template_id, client_id=sequence.client_id)
            .one_or_none()
        )
        if (
            template is None
            or template.retired_at is not None
            or template.approval_status != "approved"
        ):
            raise ValueError(
                f"step {position} template must be tenant-approved and active"
            )
        parameters = item.get("parameters", [])
        if not isinstance(parameters, (list, dict)):
            raise ValueError(f"step {position} parameters must be a list or object")
        session.add(
            WhatsAppSequenceStep(
                sequence_id=sequence.id,
                position=position,
                delay_seconds=delay_seconds,
                template_id=template_id,
                parameters=parameters,
            )
        )


def create_sequence(client_id: int, name: str, steps: list[dict[str, Any]]) -> dict:
    if not name.strip():
        raise ValueError("name is required")
    with _factory()() as session:
        sequence = WhatsAppSequence(
            client_id=client_id, name=name.strip(), status="draft"
        )
        session.add(sequence)
        session.flush()
        _replace_steps(session, sequence, steps)
        session.commit()
        session.refresh(sequence)
        return _sequence_dict(sequence)


def list_sequences(client_id: int) -> list[dict]:
    with _factory()() as session:
        rows = (
            session.query(WhatsAppSequence)
            .options(joinedload(WhatsAppSequence.steps))
            .filter_by(client_id=client_id)
            .order_by(WhatsAppSequence.id.desc())
            .all()
        )
        return [_sequence_dict(row) for row in rows]


def edit_draft(
    client_id: int,
    sequence_id: int,
    name: str | None,
    steps: list[dict[str, Any]] | None,
) -> dict:
    with _factory()() as session:
        sequence = (
            session.query(WhatsAppSequence)
            .options(joinedload(WhatsAppSequence.steps))
            .filter_by(id=sequence_id, client_id=client_id)
            .with_for_update()
            .one_or_none()
        )
        if sequence is None:
            raise LookupError("Sequence not found")
        if sequence.status != "draft":
            raise ValueError("Only draft sequences can be edited")
        if name is not None:
            if not name.strip():
                raise ValueError("name cannot be empty")
            sequence.name = name.strip()
        if steps is not None:
            _replace_steps(session, sequence, steps)
        sequence.updated_at = _now()
        session.commit()
        session.refresh(sequence)
        return _sequence_dict(sequence)


def set_sequence_status(client_id: int, sequence_id: int, operation: str) -> dict:
    expected = {
        "activate": ("active", {"draft", "paused"}),
        "pause": ("paused", {"active"}),
        "resume": ("active", {"paused"}),
        "archive": ("archived", {"draft", "paused"}),
    }
    if operation not in expected:
        raise ValueError("Unknown sequence operation")
    target, allowed = expected[operation]
    with _factory()() as session:
        sequence = (
            session.query(WhatsAppSequence)
            .options(joinedload(WhatsAppSequence.steps))
            .filter_by(id=sequence_id, client_id=client_id)
            .with_for_update()
            .one_or_none()
        )
        if sequence is None:
            raise LookupError("Sequence not found")
        if sequence.status not in allowed:
            raise ValueError(f"Cannot {operation} a {sequence.status} sequence")
        if target == "active" and not sequence.steps:
            raise ValueError("Cannot activate a sequence without steps")
        sequence.status = target
        sequence.updated_at = _now()
        session.commit()
        session.refresh(sequence)
        return _sequence_dict(sequence)


def enroll(client_id: int, sequence_id: int, lead_ids: list[int]) -> dict:
    if not lead_ids:
        raise ValueError("lead_ids are required")
    now = _now()
    enrolled: list[int] = []
    skipped: list[dict] = []
    with _factory()() as session:
        sequence = (
            session.query(WhatsAppSequence)
            .options(joinedload(WhatsAppSequence.steps))
            .filter_by(id=sequence_id, client_id=client_id)
            .with_for_update()
            .one_or_none()
        )
        if sequence is None:
            raise LookupError("Sequence not found")
        if sequence.status != "active":
            raise ValueError("Sequence must be active to enroll leads")
        for lead_id in lead_ids:
            lead = (
                session.query(Lead)
                .filter_by(id=lead_id, client_id=client_id)
                .one_or_none()
            )
            if lead is None:
                skipped.append({"lead_id": lead_id, "reason": "not_found"})
                continue
            existing = (
                session.query(WhatsAppSequenceEnrollment)
                .filter_by(sequence_id=sequence.id, lead_id=lead.id)
                .one_or_none()
            )
            if existing is not None:
                skipped.append(
                    {"lead_id": lead_id, "reason": "already_enrolled_or_terminal"}
                )
                continue
            row = WhatsAppSequenceEnrollment(
                sequence_id=sequence.id,
                lead_id=lead.id,
                client_id=client_id,
                status="active",
                current_step=0,
                next_run_at=now + timedelta(seconds=sequence.steps[0].delay_seconds),
                enrolled_at=now,
            )
            session.add(row)
            session.flush()
            enrolled.append(row.id)
        session.commit()
    return {"sequence_id": sequence_id, "enrolled_ids": enrolled, "skipped": skipped}


def list_enrollments(client_id: int, sequence_id: int) -> dict:
    with _factory()() as session:
        if (
            not session.query(WhatsAppSequence.id)
            .filter_by(id=sequence_id, client_id=client_id)
            .first()
        ):
            raise LookupError("Sequence not found")
        rows = (
            session.query(WhatsAppSequenceEnrollment, Lead)
            .join(Lead, Lead.id == WhatsAppSequenceEnrollment.lead_id)
            .filter(
                WhatsAppSequenceEnrollment.sequence_id == sequence_id,
                WhatsAppSequenceEnrollment.client_id == client_id,
            )
            .order_by(WhatsAppSequenceEnrollment.id.desc())
            .all()
        )
        return {
            "sequence_id": sequence_id,
            "enrollments": [_enrollment_dict(e, lead) for e, lead in rows],
        }


def set_enrollment_status(client_id: int, enrollment_id: int, operation: str) -> dict:
    with _factory()() as session:
        row = (
            session.query(WhatsAppSequenceEnrollment)
            .filter_by(id=enrollment_id, client_id=client_id)
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Enrollment not found")
        if operation == "pause" and row.status == "active":
            row.status, row.next_run_at = "paused", None
        elif operation == "resume" and row.status == "paused":
            row.status, row.next_run_at = "active", _now()
        elif operation == "cancel" and row.status in {"active", "paused"}:
            _stop(row, "cancelled")
        else:
            raise ValueError(f"Cannot {operation} a {row.status} enrollment")
        row.updated_at = _now()
        session.commit()
        session.refresh(row)
        return _enrollment_dict(row, session.get(Lead, row.lead_id))


def _stop(row: WhatsAppSequenceEnrollment, reason: str) -> None:
    row.status, row.stop_reason, row.next_run_at, row.updated_at = (
        "stopped",
        reason,
        None,
        _now(),
    )


def _automatic_stop_reason(
    session, enrollment: WhatsAppSequenceEnrollment, lead: Lead
) -> str | None:
    if lead.is_human_takeover:
        return "human_takeover"
    if (lead.status or "").strip().lower() in {"booked", "lost"}:
        return (lead.status or "").strip().lower()
    if (
        session.query(WhatsAppOptOut.id)
        .filter_by(client_id=enrollment.client_id, phone=lead.phone)
        .first()
        or lead.whatsapp_opted_out_at
    ):
        return "opt_out"
    inbound = (
        session.query(Message.id)
        .filter(
            Message.lead_id == lead.id,
            Message.channel == "whatsapp",
            Message.direction == "INBOUND",
            Message.created_at >= enrollment.enrolled_at,
        )
        .first()
    )
    return "inbound_reply" if inbound else None


def dry_run(client_id: int, enrollment_id: int, now: datetime | None = None) -> dict:
    with _factory()() as session:
        enrollment = (
            session.query(WhatsAppSequenceEnrollment)
            .filter_by(id=enrollment_id, client_id=client_id)
            .one_or_none()
        )
        if enrollment is None:
            raise LookupError("Enrollment not found")
        sequence = session.get(WhatsAppSequence, enrollment.sequence_id)
        lead = session.get(Lead, enrollment.lead_id)
        reason = _automatic_stop_reason(session, enrollment, lead)
        step = (
            session.query(WhatsAppSequenceStep)
            .filter_by(sequence_id=sequence.id, position=enrollment.current_step)
            .one_or_none()
        )
        return {
            "dry_run": True,
            "would_send": bool(
                sequence.status == "active"
                and enrollment.status == "active"
                and not reason
                and step
            ),
            "stop_reason": reason,
            "template_id": step.template_id if step else None,
        }


def _claim_due_enrollment(now: datetime) -> int | None:
    with _factory()() as session:
        enrollment = (
            session.query(WhatsAppSequenceEnrollment)
            .filter(
                WhatsAppSequenceEnrollment.status == "active",
                WhatsAppSequenceEnrollment.next_run_at.isnot(None),
                WhatsAppSequenceEnrollment.next_run_at <= now,
            )
            .order_by(WhatsAppSequenceEnrollment.next_run_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if enrollment is None:
            return None
        # A durable per-step claim is committed before contacting Meta. A duplicate
        # scheduler/worker can therefore never issue a second provider send.
        existing = (
            session.query(WhatsAppSequenceExecution)
            .filter_by(
                enrollment_id=enrollment.id,
                step_position=enrollment.current_step,
                attempt_number=enrollment.failure_count + 1,
            )
            .one_or_none()
        )
        if existing is not None:
            # Another worker owns this durable claim. It must not alter the
            # enrollment or emit a second provider call.
            return None
        execution = WhatsAppSequenceExecution(
            enrollment_id=enrollment.id,
            client_id=enrollment.client_id,
            step_position=enrollment.current_step,
            attempt_number=enrollment.failure_count + 1,
            state="sending",
        )
        session.add(execution)
        session.commit()
        return execution.id


def _final_sequence_guard(
    session, client, lead, *, enrollment_id: int, sequence_id: int
) -> str | None:
    """Last sequence-state/reply check while Phase 7 owns the lead lock."""
    enrollment = (
        session.query(WhatsAppSequenceEnrollment)
        .filter_by(id=enrollment_id, client_id=client.id)
        .with_for_update()
        .one_or_none()
    )
    sequence = (
        session.query(WhatsAppSequence)
        .filter_by(id=sequence_id, client_id=client.id)
        .with_for_update()
        .one_or_none()
    )
    if enrollment is None or sequence is None:
        return "enrollment_inactive"
    if enrollment.status != "active":
        return enrollment.stop_reason or "enrollment_inactive"
    if sequence.status != "active":
        _stop(enrollment, "sequence_paused")
        return "sequence_paused"
    reason = _automatic_stop_reason(session, enrollment, lead)
    if reason:
        _stop(enrollment, reason)
        return reason
    return None


def _process_claim(execution_id: int, now: datetime) -> str:
    """Execute one claimed step without holding sequence locks over Meta I/O."""
    with _factory()() as session:
        execution = session.get(WhatsAppSequenceExecution, execution_id)
        if execution is None or execution.state != "sending":
            return "skipped"
        enrollment = (
            session.query(WhatsAppSequenceEnrollment)
            .filter_by(id=execution.enrollment_id)
            .with_for_update()
            .one()
        )
        sequence = (
            session.query(WhatsAppSequence)
            .filter_by(id=enrollment.sequence_id, client_id=enrollment.client_id)
            .with_for_update()
            .one()
        )
        lead = (
            session.query(Lead)
            .filter_by(id=enrollment.lead_id, client_id=enrollment.client_id)
            .one()
        )
        if sequence.status != "active" or enrollment.status != "active":
            _stop(
                enrollment,
                "sequence_paused" if sequence.status != "active" else "manual_pause",
            )
            execution.state = "blocked"
            session.commit()
            return "stopped"
        reason = _automatic_stop_reason(session, enrollment, lead)
        if reason:
            _stop(enrollment, reason)
            execution.state = "blocked"
            session.commit()
            return "stopped"
        step = (
            session.query(WhatsAppSequenceStep)
            .filter_by(sequence_id=sequence.id, position=enrollment.current_step)
            .one_or_none()
        )
        template = session.get(WhatsAppTemplate, step.template_id) if step else None
        if (
            step is None
            or template is None
            or template.client_id != enrollment.client_id
        ):
            _stop(enrollment, "template_unapproved")
            execution.state = "blocked"
            session.commit()
            return "stopped"
        client_id, phone, sequence_id, enrollment_id = (
            enrollment.client_id,
            lead.phone,
            sequence.id,
            enrollment.id,
        )
        template_id, template_name, language, parameters = (
            template.id,
            template.name,
            template.language,
            step.parameters,
        )
        session.commit()

    try:
        from app.api.runtime import whatsapp

        result = whatsapp_policy.send_immediate_template(
            client_id=client_id,
            phone=phone,
            template_name=template_name,
            language=language,
            template_id=template_id,
            parameters=parameters,
            sender=whatsapp.send_template,
            action="sequence_step_send",
            final_guard=lambda policy_session,
            policy_client,
            policy_lead: _final_sequence_guard(
                policy_session,
                policy_client,
                policy_lead,
                enrollment_id=enrollment_id,
                sequence_id=sequence_id,
            ),
        )
    except whatsapp_policy.ProviderOutcomeUncertain:
        with _factory()() as session:
            execution = session.get(WhatsAppSequenceExecution, execution_id)
            enrollment = (
                session.query(WhatsAppSequenceEnrollment)
                .filter_by(id=enrollment_id)
                .with_for_update()
                .one()
            )
            execution.state = "unknown"
            execution.completed_at = now
            _stop(enrollment, "provider_outcome_uncertain")
            session.commit()
        return "stopped"
    except Exception as exc:
        if whatsapp_outbox._send_failure_is_uncertain(
            exc,
            provider_accepted=False,
        ):
            with _factory()() as session:
                execution = session.get(WhatsAppSequenceExecution, execution_id)
                enrollment = (
                    session.query(WhatsAppSequenceEnrollment)
                    .filter_by(id=enrollment_id)
                    .with_for_update()
                    .one()
                )
                execution.state = "unknown"
                execution.completed_at = now
                _stop(enrollment, "provider_outcome_uncertain")
                session.commit()
            return "stopped"
        with _factory()() as session:
            execution = session.get(WhatsAppSequenceExecution, execution_id)
            enrollment = (
                session.query(WhatsAppSequenceEnrollment)
                .filter_by(id=enrollment_id)
                .with_for_update()
                .one()
            )
            enrollment.failure_count += 1
            enrollment.last_run_at = now
            execution.state = "failed"
            execution.completed_at = now
            if enrollment.failure_count >= FAILURE_THRESHOLD:
                _stop(enrollment, "provider_failure_threshold")
            else:
                enrollment.next_run_at = now + _RETRY_DELAY
            session.commit()
        return "failed"

    with _factory()() as session:
        execution = session.get(WhatsAppSequenceExecution, execution_id)
        enrollment = (
            session.query(WhatsAppSequenceEnrollment)
            .filter_by(id=enrollment_id)
            .with_for_update()
            .one()
        )
        lead = (
            session.query(Lead)
            .filter_by(id=enrollment.lead_id, client_id=client_id)
            .one()
        )
        enrollment.last_run_at = now
        execution.completed_at = now
        if result.state != "sent":
            execution.state = "blocked"
            if result.reason_code in {
                "opted_out",
                "human_takeover",
                "lead_stage_excluded",
                "consent_absent",
                "consent_revoked",
                "template_unapproved",
                "global_kill_switch",
                "tenant_kill_switch",
                "inbound_reply",
                "sequence_paused",
                "enrollment_inactive",
            }:
                _stop(enrollment, result.reason_code)
            else:
                enrollment.next_run_at = now + _RETRY_DELAY
            session.commit()
            return "blocked"
        execution.state, execution.provider_message_id = (
            "sent",
            result.provider_message_id,
        )
        session.add(
            Message(
                lead_id=lead.id,
                direction="OUTBOUND",
                msg_type="template",
                body=f"[template: {template_name}]",
                channel="whatsapp",
                wa_message_id=result.provider_message_id,
                status="sent",
            )
        )
        enrollment.current_step += 1
        next_step = (
            session.query(WhatsAppSequenceStep)
            .filter_by(sequence_id=sequence_id, position=enrollment.current_step)
            .one_or_none()
        )
        if next_step is None:
            enrollment.status, enrollment.next_run_at = "completed", None
        else:
            enrollment.next_run_at = now + timedelta(seconds=next_step.delay_seconds)
        session.commit()
        return "sent"


def process_due_enrollments(
    limit: int = _MAX_DUE_PER_TICK,
    *,
    should_continue: Callable[[], bool] | None = None,
) -> dict:
    now = _now()
    stats = {
        "processed": 0,
        "sent": 0,
        "stopped": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
    }
    for _ in range(limit):
        if should_continue is not None and not should_continue():
            stats["skipped"] += 1
            break
        execution_id = _claim_due_enrollment(now)
        if execution_id is None:
            break
        outcome = _process_claim(execution_id, now)
        stats["processed"] += 1
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats


def _renew_scheduler_lock(lock, stop: threading.Event, lost: threading.Event) -> None:
    while not stop.wait(_SCHEDULER_RENEW_INTERVAL_SECONDS):
        try:
            if not lock.owned():
                lost.set()
                return
            lock.extend(_SCHEDULER_LOCK_SECONDS, replace_ttl=True)
        except Exception:  # noqa: BLE001 - any Redis failure forfeits ownership
            lost.set()
            logger.exception("WhatsApp sequence scheduler lock renewal failed")
            return


def run_sequence_tick_job() -> dict:
    """RQ-only periodic entrypoint; reschedules itself through Redis, never web."""
    from app.api.runtime import webhook_queue

    if webhook_queue is None:
        return {"skipped": "queue_unavailable"}
    lock = webhook_queue.connection.lock(
        _SCHEDULER_LOCK_KEY,
        timeout=_SCHEDULER_LOCK_SECONDS,
        blocking_timeout=0,
    )
    stop_renewal = threading.Event()
    ownership_lost = threading.Event()
    renewer: threading.Thread | None = None
    try:
        if not lock.acquire(blocking=False):
            return {"skipped": "scheduler_locked"}
        renewer = threading.Thread(
            target=_renew_scheduler_lock,
            args=(lock, stop_renewal, ownership_lost),
            name="whatsapp-sequence-lock-renewer",
            daemon=True,
        )
        renewer.start()
        result = process_due_enrollments(
            should_continue=lambda: (
                not ownership_lost.is_set() and lock.owned()
            )
        )
        if ownership_lost.is_set() or not lock.owned():
            return {"skipped": "scheduler_lock_lost"}
        try:
            lock.extend(_SCHEDULER_LOCK_SECONDS, replace_ttl=True)
        except LockError:
            return {"skipped": "scheduler_lock_lost"}
        webhook_queue.enqueue_in(
            timedelta(minutes=1), run_sequence_tick_job, job_id=_SCHEDULER_JOB_ID
        )
        return result
    finally:
        stop_renewal.set()
        if renewer is not None:
            renewer.join(timeout=1)
        try:
            lock.release()
        except LockError:
            pass

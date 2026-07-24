"""
Email campaign sequences — Phase E7.

- Create campaigns with ordered steps (subject/body templates + delay_hours)
- Enroll leads; scheduler advances due enrollments
- Stop on: inbound reply, unsub, bounce/complaint, booked/lost, takeover, suppressed
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.core.config import EMAIL_DEFAULT_FROM_ADDRESS, EMAIL_DEFAULT_FROM_NAME
from app.core.database import SessionLocal, is_configured
from app.email.email_client import EmailSendError, email_client
from app.email.email_templates import (
    apply_merge_fields,
    build_unsubscribe_url,
    wrap_email_bodies,
)
from app.email.email_validation import validate_lead_email
from app.core.models import (
    Client,
    EmailCampaign,
    EmailCampaignEnrollment,
    EmailCampaignStep,
    EmailSuppression,
    Lead,
    Message,
    PipelineStage,
)
from app.services.usage import check_limit, log_usage

logger = logging.getLogger(__name__)

CAMPAIGN_STATUSES = frozenset({"draft", "active", "paused", "archived"})
ENROLLMENT_STATUSES = frozenset({"active", "paused", "completed", "stopped"})

# Cap work per scheduler tick
_MAX_DUE_PER_TICK = 50


def _utcnow() -> datetime:
    return datetime.utcnow()


def check_stop_conditions(
    session,
    *,
    lead: Lead,
    enrollment: EmailCampaignEnrollment,
    client: Client,
) -> str | None:
    """
    Return stop_reason if enrollment should stop, else None.
    """
    if lead.is_human_takeover:
        return "takeover"

    status_norm = (lead.email_status or "").strip().lower()
    if status_norm == "unsubscribed":
        return "unsubscribed"
    if status_norm == "bounced":
        return "bounce"
    if status_norm == "complained":
        return "complaint"

    email = (lead.email or "").strip().lower()
    if not email:
        return "no_email"

    sup = (
        session.query(EmailSuppression)
        .filter(
            EmailSuppression.client_id == enrollment.client_id,
            EmailSuppression.email == email,
        )
        .first()
    )
    if sup:
        if sup.reason == "unsubscribed":
            return "unsubscribed"
        if sup.reason == "complaint":
            return "complaint"
        return "suppressed"

    # Pipeline won/lost
    won = {
        s.name
        for s in session.query(PipelineStage)
        .filter(
            PipelineStage.client_id == enrollment.client_id,
            PipelineStage.is_won.is_(True),
        )
        .all()
    }
    lost = {
        s.name
        for s in session.query(PipelineStage)
        .filter(
            PipelineStage.client_id == enrollment.client_id,
            PipelineStage.is_lost.is_(True),
        )
        .all()
    }
    if not won:
        won = {"Booked"}
    if not lost:
        lost = {"Lost"}

    if (lead.status or "") in won:
        return "booked"
    if (lead.status or "") in lost:
        return "lost"

    # Inbound email reply after enrollment
    inbound = (
        session.query(Message.id)
        .filter(
            Message.lead_id == lead.id,
            Message.channel == "email",
            Message.direction == "INBOUND",
            Message.created_at >= enrollment.enrolled_at,
        )
        .first()
    )
    if inbound:
        return "reply"

    return None


def _stop_enrollment(
    enrollment: EmailCampaignEnrollment,
    reason: str,
) -> None:
    enrollment.status = "stopped"
    enrollment.stop_reason = reason
    enrollment.next_run_at = None
    enrollment.updated_at = _utcnow()


def create_campaign(
    client_id: int,
    name: str,
    steps: list[dict[str, Any]] | None = None,
) -> dict:
    if not is_configured() or not SessionLocal:
        raise RuntimeError("Database not configured")
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")

    with SessionLocal() as session:
        campaign = EmailCampaign(
            client_id=client_id,
            name=name,
            status="draft",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        session.add(campaign)
        session.flush()

        if steps:
            _replace_steps(session, campaign, steps)

        session.commit()
        session.refresh(campaign)
        return _campaign_dict(session, campaign)


def list_campaigns(client_id: int) -> list[dict]:
    if not is_configured() or not SessionLocal:
        return []
    with SessionLocal() as session:
        rows = (
            session.query(EmailCampaign)
            .filter(EmailCampaign.client_id == client_id)
            .order_by(EmailCampaign.id.desc())
            .all()
        )
        return [_campaign_dict(session, c) for c in rows]


def get_campaign(client_id: int, campaign_id: int) -> dict | None:
    if not is_configured() or not SessionLocal:
        return None
    with SessionLocal() as session:
        c = (
            session.query(EmailCampaign)
            .options(joinedload(EmailCampaign.steps))
            .filter(
                EmailCampaign.id == campaign_id,
                EmailCampaign.client_id == client_id,
            )
            .first()
        )
        if not c:
            return None
        return _campaign_dict(session, c)


def update_campaign(
    client_id: int,
    campaign_id: int,
    *,
    name: str | None = None,
    status: str | None = None,
) -> dict:
    if not is_configured() or not SessionLocal:
        raise RuntimeError("Database not configured")
    with SessionLocal() as session:
        c = (
            session.query(EmailCampaign)
            .filter(
                EmailCampaign.id == campaign_id,
                EmailCampaign.client_id == client_id,
            )
            .first()
        )
        if not c:
            raise LookupError("Campaign not found")
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("name cannot be empty")
            c.name = name
        if status is not None:
            if status not in CAMPAIGN_STATUSES:
                raise ValueError(f"status must be one of {sorted(CAMPAIGN_STATUSES)}")
            if status == "active":
                step_count = (
                    session.query(func.count(EmailCampaignStep.id))
                    .filter(EmailCampaignStep.campaign_id == c.id)
                    .scalar()
                    or 0
                )
                if step_count < 1:
                    raise ValueError("Cannot activate campaign with zero steps")
            c.status = status
        c.updated_at = _utcnow()
        session.commit()
        session.refresh(c)
        return _campaign_dict(session, c)


def set_campaign_steps(
    client_id: int,
    campaign_id: int,
    steps: list[dict[str, Any]],
) -> dict:
    if not is_configured() or not SessionLocal:
        raise RuntimeError("Database not configured")
    with SessionLocal() as session:
        c = (
            session.query(EmailCampaign)
            .filter(
                EmailCampaign.id == campaign_id,
                EmailCampaign.client_id == client_id,
            )
            .first()
        )
        if not c:
            raise LookupError("Campaign not found")
        if c.status == "active":
            # Allow replace but warn via requiring pause — safer
            raise ValueError("Pause the campaign before editing steps")
        _replace_steps(session, c, steps)
        c.updated_at = _utcnow()
        session.commit()
        session.refresh(c)
        return _campaign_dict(session, c)


def _replace_steps(session, campaign: EmailCampaign, steps: list[dict[str, Any]]) -> None:
    if not steps:
        raise ValueError("steps cannot be empty")
    session.query(EmailCampaignStep).filter(
        EmailCampaignStep.campaign_id == campaign.id
    ).delete()
    for i, step in enumerate(steps):
        subject = (step.get("subject_template") or step.get("subject") or "").strip()
        body = (step.get("body_template") or step.get("body_text") or step.get("body") or "").strip()
        if not subject or not body:
            raise ValueError(f"step {i}: subject_template and body_template are required")
        delay = int(step.get("delay_hours", 0) or 0)
        if delay < 0:
            raise ValueError(f"step {i}: delay_hours must be >= 0")
        if len(subject) > 500:
            raise ValueError(f"step {i}: subject_template too long")
        session.add(
            EmailCampaignStep(
                campaign_id=campaign.id,
                position=i,
                delay_hours=delay,
                subject_template=subject,
                body_template=body,
            )
        )


def enroll_leads(
    client_id: int,
    campaign_id: int,
    lead_ids: list[int],
) -> dict:
    if not is_configured() or not SessionLocal:
        raise RuntimeError("Database not configured")
    if not lead_ids:
        raise ValueError("lead_ids is required")

    with SessionLocal() as session:
        campaign = (
            session.query(EmailCampaign)
            .options(joinedload(EmailCampaign.steps))
            .filter(
                EmailCampaign.id == campaign_id,
                EmailCampaign.client_id == client_id,
            )
            .first()
        )
        if not campaign:
            raise LookupError("Campaign not found")
        if campaign.status != "active":
            raise ValueError("Campaign must be active to enroll leads")
        if not campaign.steps:
            raise ValueError("Campaign has no steps")

        first_delay = campaign.steps[0].delay_hours or 0
        now = _utcnow()
        enrolled = []
        skipped = []

        for lid in lead_ids:
            lead = (
                session.query(Lead)
                .filter(Lead.id == int(lid), Lead.client_id == client_id)
                .first()
            )
            if not lead:
                skipped.append({"lead_id": lid, "reason": "not_found"})
                continue
            email_check = validate_lead_email(lead.email, allow_empty=False)
            if not email_check.ok:
                skipped.append(
                    {"lead_id": lid, "reason": email_check.error or "invalid_email"}
                )
                continue

            existing = (
                session.query(EmailCampaignEnrollment)
                .filter(
                    EmailCampaignEnrollment.campaign_id == campaign_id,
                    EmailCampaignEnrollment.lead_id == lead.id,
                )
                .first()
            )
            if existing:
                if existing.status in ("active", "paused"):
                    skipped.append({"lead_id": lid, "reason": "already_enrolled"})
                    continue
                # Re-enroll completed/stopped
                existing.status = "active"
                existing.current_step = 0
                existing.stop_reason = None
                existing.next_run_at = now + timedelta(hours=first_delay)
                existing.enrolled_at = now
                existing.updated_at = now
                existing.last_sent_at = None
                enrolled.append(existing.id)
                continue

            # One active primary campaign at a time (product rule)
            other_active = (
                session.query(EmailCampaignEnrollment)
                .filter(
                    EmailCampaignEnrollment.lead_id == lead.id,
                    EmailCampaignEnrollment.status == "active",
                    EmailCampaignEnrollment.campaign_id != campaign_id,
                )
                .first()
            )
            if other_active:
                skipped.append(
                    {
                        "lead_id": lid,
                        "reason": "already_in_another_campaign",
                        "other_campaign_id": other_active.campaign_id,
                    }
                )
                continue

            enr = EmailCampaignEnrollment(
                campaign_id=campaign_id,
                lead_id=lead.id,
                client_id=client_id,
                status="active",
                current_step=0,
                next_run_at=now + timedelta(hours=first_delay),
                enrolled_at=now,
                updated_at=now,
            )
            session.add(enr)
            session.flush()
            enrolled.append(enr.id)

        session.commit()
        return {
            "campaign_id": campaign_id,
            "enrolled_ids": enrolled,
            "enrolled_count": len(enrolled),
            "skipped": skipped,
        }


def _enrollment_dict(enr: EmailCampaignEnrollment, lead: Lead | None = None) -> dict:
    """Serialize enrollment for list/detail responses (no secrets)."""
    return {
        "id": enr.id,
        "campaign_id": enr.campaign_id,
        "lead_id": enr.lead_id,
        "client_id": enr.client_id,
        "status": enr.status,
        "current_step": enr.current_step,
        "next_run_at": enr.next_run_at.isoformat() if enr.next_run_at else None,
        "stop_reason": enr.stop_reason,
        "last_sent_at": enr.last_sent_at.isoformat() if enr.last_sent_at else None,
        "enrolled_at": enr.enrolled_at.isoformat() if enr.enrolled_at else None,
        "updated_at": enr.updated_at.isoformat() if enr.updated_at else None,
        "lead_name": (lead.name if lead else None) or None,
        "lead_email": (lead.email if lead else None) or None,
        "lead_phone": (lead.phone if lead else None) or None,
    }


def list_enrollments(client_id: int, campaign_id: int) -> dict:
    """
    List enrollments for a campaign (tenant-scoped).
    Fills the dashboard gap: pause/resume need known enrollment ids.
    """
    if not is_configured() or not SessionLocal:
        return {"campaign_id": campaign_id, "enrollments": [], "count": 0}

    with SessionLocal() as session:
        campaign = (
            session.query(EmailCampaign)
            .filter(
                EmailCampaign.id == campaign_id,
                EmailCampaign.client_id == client_id,
            )
            .first()
        )
        if not campaign:
            raise LookupError("Campaign not found")

        rows = (
            session.query(EmailCampaignEnrollment, Lead)
            .outerjoin(Lead, Lead.id == EmailCampaignEnrollment.lead_id)
            .filter(
                EmailCampaignEnrollment.campaign_id == campaign_id,
                EmailCampaignEnrollment.client_id == client_id,
            )
            .order_by(EmailCampaignEnrollment.id.desc())
            .all()
        )
        enrollments = [_enrollment_dict(enr, lead) for enr, lead in rows]
        return {
            "campaign_id": campaign_id,
            "enrollments": enrollments,
            "count": len(enrollments),
        }


def pause_enrollment(client_id: int, enrollment_id: int) -> dict:
    return _set_enrollment_status(client_id, enrollment_id, "paused")


def resume_enrollment(client_id: int, enrollment_id: int) -> dict:
    return _set_enrollment_status(client_id, enrollment_id, "active")


def _set_enrollment_status(client_id: int, enrollment_id: int, status: str) -> dict:
    if not is_configured() or not SessionLocal:
        raise RuntimeError("Database not configured")
    with SessionLocal() as session:
        enr = (
            session.query(EmailCampaignEnrollment)
            .filter(
                EmailCampaignEnrollment.id == enrollment_id,
                EmailCampaignEnrollment.client_id == client_id,
            )
            .first()
        )
        if not enr:
            raise LookupError("Enrollment not found")
        if enr.status in ("completed", "stopped") and status == "active":
            raise ValueError("Cannot resume a completed/stopped enrollment; re-enroll the lead")
        if status == "paused" and enr.status not in ("active", "paused"):
            raise ValueError(f"Cannot pause enrollment with status '{enr.status}'")
        if status == "active" and enr.status not in ("paused", "active"):
            raise ValueError(f"Cannot resume enrollment with status '{enr.status}'")
        enr.status = status
        enr.updated_at = _utcnow()
        if status == "paused":
            enr.next_run_at = None
        elif status == "active" and enr.next_run_at is None:
            enr.next_run_at = _utcnow()
        session.commit()
        session.refresh(enr)
        lead = session.query(Lead).filter(Lead.id == enr.lead_id).first()
        return _enrollment_dict(enr, lead)


def process_due_enrollments(limit: int = _MAX_DUE_PER_TICK) -> dict:
    """Scheduler entry: send due campaign steps. Safe to call frequently."""
    if not is_configured() or not SessionLocal:
        return {"processed": 0, "sent": 0, "stopped": 0, "errors": 0}

    now = _utcnow()
    stats = {"processed": 0, "sent": 0, "stopped": 0, "errors": 0, "skipped": 0}

    with SessionLocal() as session:
        due = (
            session.query(EmailCampaignEnrollment)
            .filter(
                EmailCampaignEnrollment.status == "active",
                EmailCampaignEnrollment.next_run_at.isnot(None),
                EmailCampaignEnrollment.next_run_at <= now,
            )
            .order_by(EmailCampaignEnrollment.next_run_at.asc())
            .limit(limit)
            .all()
        )

        for enr in due:
            stats["processed"] += 1
            try:
                outcome = _process_one(session, enr, now)
                stats[outcome] = stats.get(outcome, 0) + 1
            except Exception as e:
                logger.error(
                    "Campaign enrollment %s failed: %s", enr.id, e, exc_info=True
                )
                stats["errors"] += 1
                enr.updated_at = _utcnow()
                # push next attempt 1h later to avoid tight error loops
                enr.next_run_at = now + timedelta(hours=1)

        session.commit()

    logger.info("Campaign tick: %s", stats)
    return stats


def _process_one(session, enr: EmailCampaignEnrollment, now: datetime) -> str:
    campaign = session.get(EmailCampaign, enr.campaign_id)
    if not campaign or campaign.client_id != enr.client_id:
        _stop_enrollment(enr, "error")
        return "stopped"
    if campaign.status != "active":
        enr.status = "paused"
        enr.stop_reason = "campaign_paused"
        enr.next_run_at = None
        enr.updated_at = now
        return "skipped"

    lead = session.get(Lead, enr.lead_id)
    client = session.get(Client, enr.client_id)
    if not lead or not client:
        _stop_enrollment(enr, "error")
        return "stopped"

    stop = check_stop_conditions(
        session, lead=lead, enrollment=enr, client=client
    )
    if stop:
        _stop_enrollment(enr, stop)
        return "stopped"

    steps = (
        session.query(EmailCampaignStep)
        .filter(EmailCampaignStep.campaign_id == campaign.id)
        .order_by(EmailCampaignStep.position.asc())
        .all()
    )
    if not steps:
        _stop_enrollment(enr, "error")
        return "stopped"

    step_map = {s.position: s for s in steps}
    step = step_map.get(enr.current_step)
    if not step:
        enr.status = "completed"
        enr.next_run_at = None
        enr.updated_at = now
        return "skipped"

    # Plan limits
    plan = client.plan_tier or "base"
    allowed, reason = check_limit(client.id, "email_sent", plan=plan)
    if not allowed:
        logger.warning("Campaign send blocked by limit: %s", reason)
        enr.next_run_at = now + timedelta(hours=6)
        enr.updated_at = now
        return "skipped"

    if not client.email_enabled or not email_client.is_ready():
        enr.next_run_at = now + timedelta(hours=1)
        enr.updated_at = now
        return "skipped"

    from_address = (client.email_from_address or EMAIL_DEFAULT_FROM_ADDRESS or "").strip()
    if not from_address:
        enr.next_run_at = now + timedelta(hours=1)
        enr.updated_at = now
        return "skipped"

    to_email = (lead.email or "").strip().lower()
    merge = {
        "name": lead.name or "",
        "business_name": lead.business_name or "",
        "email": to_email,
        "calendly_link": client.calendly_link or "",
        "company_display_name": client.company_display_name or client.name or "",
    }
    subject = apply_merge_fields(step.subject_template, merge)
    body = apply_merge_fields(step.body_template, merge)

    try:
        unsub_url = build_unsubscribe_url(client.id, to_email)
    except ValueError as e:
        logger.error("Campaign unsub URL failed: %s", e)
        enr.next_run_at = now + timedelta(hours=1)
        return "errors"

    final_text, final_html = wrap_email_bodies(
        body_text=body,
        body_html=None,
        company_address=client.email_company_address,
        unsubscribe_url=unsub_url,
        custom_footer_html=client.email_footer_html,
    )

    headers = {
        "List-Unsubscribe": f"<{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }

    try:
        result = email_client.send_email(
            to=to_email,
            subject=subject,
            text=final_text,
            html=final_html,
            from_address=from_address,
            from_name=client.email_from_name or EMAIL_DEFAULT_FROM_NAME or None,
            reply_to=client.email_reply_to or None,
            headers=headers,
            tags={
                "client_id": str(client.id),
                "lead_id": str(lead.id),
                "campaign_id": str(campaign.id),
                "enrollment_id": str(enr.id),
                "step": str(step.position),
            },
        )
    except EmailSendError as e:
        logger.error("Campaign send failed enrollment=%s: %s", enr.id, e)
        enr.next_run_at = now + timedelta(hours=1)
        enr.updated_at = now
        return "errors"

    session.add(
        Message(
            lead_id=lead.id,
            direction="OUTBOUND",
            msg_type="email",
            body=final_text,
            channel="email",
            subject=subject,
            provider_message_id=result.provider_message_id,
            status="sent",
            email_headers=headers,
            provider_metadata={
                "campaign_id": campaign.id,
                "enrollment_id": enr.id,
                "step": step.position,
                "kind": "campaign",
            },
            created_at=now,
        )
    )

    if (lead.status or "") == "New Lead":
        lead.status = "Contacted"
    if not lead.email_status:
        lead.email_status = "valid"
    lead.updated_at = now

    log_usage(client.id, "email_sent", 0, 0.0)

    enr.last_sent_at = now
    enr.updated_at = now
    next_pos = enr.current_step + 1
    next_step = step_map.get(next_pos)
    if next_step is None:
        enr.status = "completed"
        enr.current_step = next_pos
        enr.next_run_at = None
    else:
        enr.current_step = next_pos
        enr.next_run_at = now + timedelta(hours=next_step.delay_hours or 0)

    return "sent"


def campaign_analytics(client_id: int, campaign_id: int) -> dict:
    if not is_configured() or not SessionLocal:
        raise RuntimeError("Database not configured")
    with SessionLocal() as session:
        campaign = (
            session.query(EmailCampaign)
            .filter(
                EmailCampaign.id == campaign_id,
                EmailCampaign.client_id == client_id,
            )
            .first()
        )
        if not campaign:
            raise LookupError("Campaign not found")

        enrollments = (
            session.query(EmailCampaignEnrollment)
            .filter(EmailCampaignEnrollment.campaign_id == campaign_id)
            .all()
        )
        by_status: dict[str, int] = {}
        by_stop: dict[str, int] = {}
        for e in enrollments:
            by_status[e.status] = by_status.get(e.status, 0) + 1
            if e.stop_reason:
                by_stop[e.stop_reason] = by_stop.get(e.stop_reason, 0) + 1

        # Messages tagged with this campaign_id in provider_metadata
        # JSONB contains — portable enough on Postgres
        sent_msgs = (
            session.query(Message)
            .filter(
                Message.channel == "email",
                Message.direction == "OUTBOUND",
                Message.provider_metadata.contains({"campaign_id": campaign_id}),
            )
            .all()
        )
        emails_sent = len(sent_msgs)
        opened = sum(1 for m in sent_msgs if (m.status or "") == "opened")
        clicked = sum(1 for m in sent_msgs if (m.status or "") == "clicked")
        delivered = sum(
            1 for m in sent_msgs if (m.status or "") in ("delivered", "opened", "clicked")
        )

        # Replies: enrollments stopped for reply, or inbound after enroll
        replies = by_stop.get("reply", 0)

        open_rate = round((opened / emails_sent) * 100, 2) if emails_sent else 0.0
        reply_rate = (
            round((replies / len(enrollments)) * 100, 2) if enrollments else 0.0
        )

        return {
            "campaign_id": campaign_id,
            "name": campaign.name,
            "status": campaign.status,
            "enrollment_count": len(enrollments),
            "by_status": by_status,
            "by_stop_reason": by_stop,
            "emails_sent": emails_sent,
            "delivered": delivered,
            "opened": opened,
            "clicked": clicked,
            "replies": replies,
            "open_rate_pct": open_rate,
            "reply_rate_pct": reply_rate,
        }


def _campaign_dict(session, campaign: EmailCampaign) -> dict:
    steps = (
        session.query(EmailCampaignStep)
        .filter(EmailCampaignStep.campaign_id == campaign.id)
        .order_by(EmailCampaignStep.position.asc())
        .all()
    )
    enr_count = (
        session.query(func.count(EmailCampaignEnrollment.id))
        .filter(EmailCampaignEnrollment.campaign_id == campaign.id)
        .scalar()
        or 0
    )
    return {
        "id": campaign.id,
        "client_id": campaign.client_id,
        "name": campaign.name,
        "status": campaign.status,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
        "enrollment_count": enr_count,
        "steps": [
            {
                "id": s.id,
                "position": s.position,
                "delay_hours": s.delay_hours,
                "subject_template": s.subject_template,
                "body_template": s.body_template,
            }
            for s in steps
        ],
    }


def run_campaign_tick_job() -> None:
    """APScheduler entry point."""
    try:
        process_due_enrollments()
    except Exception as e:
        logger.error("Campaign tick job failed: %s", e, exc_info=True)

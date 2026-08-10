"""Canonical Postgres-backed lead stage mutations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import database
from app.core.models import Lead, LeadStageChangeAudit, PipelineStage
from app.services.dashboard_events import queue_dashboard_event


class InvalidLeadStage(ValueError):
    pass


class LeadNotFound(LookupError):
    pass


class StageStoreUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class StageChangeResult:
    lead_id: int
    old_stage: str
    new_stage: str
    changed: bool
    old_is_won: bool
    old_is_lost: bool
    is_won: bool
    is_lost: bool


def _clean_context(value: str, *, field: str, max_length: int) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > max_length:
        raise ValueError(f"{field} must be 1-{max_length} characters")
    return clean


def _resolve_stage(
    session: Session, *, client_id: int, requested: str
) -> tuple[PipelineStage, list[PipelineStage]]:
    normalized = str(requested or "").strip().casefold()
    if not normalized:
        raise InvalidLeadStage("Stage is required")
    stages = list(
        session.execute(
            select(PipelineStage)
            .where(PipelineStage.client_id == client_id)
            .order_by(PipelineStage.position)
        ).scalars().all()
    )
    for stage in stages:
        if stage.name.strip().casefold() == normalized:
            return stage, stages
    allowed = ", ".join(stage.name for stage in stages)
    raise InvalidLeadStage(f"Invalid stage. Must be one of: {allowed}")


def mutate_lead_stage(
    session: Session,
    *,
    client_id: int,
    new_stage: str,
    source: str,
    actor: str,
    lead_id: int | None = None,
    phone: str | None = None,
) -> StageChangeResult:
    """Change one tenant lead stage and append audit in caller transaction."""
    if client_id is None or (lead_id is None) == (phone is None):
        raise ValueError("Exactly one tenant-scoped lead locator is required")
    source = _clean_context(source, field="source", max_length=100)
    actor = _clean_context(actor, field="actor", max_length=120)
    target, stages = _resolve_stage(
        session, client_id=client_id, requested=new_stage
    )

    query = select(Lead).where(Lead.client_id == client_id)
    query = (
        query.where(Lead.id == lead_id)
        if lead_id is not None
        else query.where(Lead.phone == phone)
    )
    lead = session.execute(query.with_for_update()).scalar_one_or_none()
    if lead is None:
        raise LeadNotFound("Lead not found")

    old_stage = str(lead.status or "").strip()
    old_definition = next(
        (
            stage
            for stage in stages
            if stage.name.strip().casefold() == old_stage.casefold()
        ),
        None,
    )
    old_is_won = bool(old_definition and old_definition.is_won)
    old_is_lost = bool(old_definition and old_definition.is_lost)
    if old_stage == target.name:
        return StageChangeResult(
            lead_id=lead.id,
            old_stage=old_stage,
            new_stage=target.name,
            changed=False,
            old_is_won=old_is_won,
            old_is_lost=old_is_lost,
            is_won=bool(target.is_won),
            is_lost=bool(target.is_lost),
        )

    lead.status = target.name
    lead.updated_at = datetime.now(timezone.utc)
    session.add(
        LeadStageChangeAudit(
            client_id=client_id,
            lead_id=lead.id,
            old_stage=old_stage,
            new_stage=target.name,
            source=source,
            actor=actor,
        )
    )
    session.flush()
    queue_dashboard_event(
        session,
        "lead_stage_changed",
        client_id=client_id,
        lead_id=lead.id,
        previous_stage=old_stage,
        new_stage=target.name,
    )
    return StageChangeResult(
        lead_id=lead.id,
        old_stage=old_stage,
        new_stage=target.name,
        changed=True,
        old_is_won=old_is_won,
        old_is_lost=old_is_lost,
        is_won=bool(target.is_won),
        is_lost=bool(target.is_lost),
    )


def change_lead_stage(
    *,
    client_id: int,
    new_stage: str,
    source: str,
    actor: str,
    lead_id: int | None = None,
    phone: str | None = None,
) -> StageChangeResult:
    """Own transaction for workers/routes without an existing session."""
    if not database.is_configured() or database.SessionLocal is None:
        raise StageStoreUnavailable("Postgres stage store is unavailable")
    with database.SessionLocal() as session:
        result = mutate_lead_stage(
            session,
            client_id=client_id,
            lead_id=lead_id,
            phone=phone,
            new_stage=new_stage,
            source=source,
            actor=actor,
        )
        session.commit()
        return result

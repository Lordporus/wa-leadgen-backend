from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    LeadStageChangeAudit,
    PipelineStage,
)
from app.services.lead_stage import (
    InvalidLeadStage,
    LeadNotFound,
    mutate_lead_stage,
)


@pytest.fixture
def stage_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Client.__table__,
            Lead.__table__,
            PipelineStage.__table__,
            LeadStageChangeAudit.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add_all(
            [
                Client(id=1, name="Tenant 1"),
                Client(id=2, name="Tenant 2"),
                PipelineStage(
                    client_id=1, name="New Lead", position=0,
                    is_won=False, is_lost=False,
                ),
                PipelineStage(
                    client_id=1, name="Booked", position=1,
                    is_won=True, is_lost=False,
                ),
                PipelineStage(
                    client_id=1, name="Lost", position=2,
                    is_won=False, is_lost=True,
                ),
                PipelineStage(
                    client_id=2, name="New Lead", position=0,
                    is_won=False, is_lost=False,
                ),
                Lead(
                    id=10, client_id=1, phone="15550000001",
                    name="Tenant 1 Lead", status="New Lead",
                ),
                Lead(
                    id=20, client_id=2, phone="15550000002",
                    name="Tenant 2 Lead", status="New Lead",
                ),
            ]
        )
        session.commit()
        yield session


def test_stage_mutation_normalizes_classifies_and_audits(stage_session):
    result = mutate_lead_stage(
        stage_session,
        client_id=1,
        lead_id=10,
        new_stage="  booked ",
        source="api:kanban",
        actor="tenant:1:authenticated-session",
    )
    stage_session.commit()

    assert result.old_stage == "New Lead"
    assert result.new_stage == "Booked"
    assert result.changed is True
    assert result.old_is_won is False
    assert result.old_is_lost is False
    assert result.is_won is True
    assert result.is_lost is False
    assert stage_session.get(Lead, 10).status == "Booked"

    audit = stage_session.execute(
        select(LeadStageChangeAudit)
    ).scalar_one()
    assert (
        audit.client_id,
        audit.lead_id,
        audit.old_stage,
        audit.new_stage,
        audit.source,
        audit.actor,
    ) == (
        1,
        10,
        "New Lead",
        "Booked",
        "api:kanban",
        "tenant:1:authenticated-session",
    )
    assert audit.created_at is not None


def test_stage_mutation_rejects_cross_tenant_and_invalid_stage(stage_session):
    with pytest.raises(LeadNotFound):
        mutate_lead_stage(
            stage_session,
            client_id=2,
            lead_id=10,
            new_stage="New Lead",
            source="test",
            actor="test",
        )

    with pytest.raises(InvalidLeadStage):
        mutate_lead_stage(
            stage_session,
            client_id=1,
            lead_id=10,
            new_stage="Manual Test Stage",
            source="test",
            actor="test",
        )

    assert stage_session.get(Lead, 10).status == "New Lead"
    assert stage_session.query(LeadStageChangeAudit).count() == 0


def test_same_stage_is_idempotent_without_duplicate_audit(stage_session):
    result = mutate_lead_stage(
        stage_session,
        client_id=1,
        phone="15550000001",
        new_stage="New Lead",
        source="worker",
        actor="system:test",
    )
    stage_session.commit()

    assert result.changed is False
    assert stage_session.query(LeadStageChangeAudit).count() == 0


def test_no_direct_lead_status_assignment_outside_canonical_service():
    backend = Path(__file__).resolve().parents[2]
    offenders = []
    for path in (backend / "app").rglob("*.py"):
        if path.name == "lead_stage.py":
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            if "lead.status =" in line:
                offenders.append(f"{path.relative_to(backend)}:{line_number}")
    assert offenders == []

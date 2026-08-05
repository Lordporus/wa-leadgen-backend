from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routers import whatsapp_dead_letters as routes
from app.core import database
from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    WhatsAppOperationalControl,
    WhatsAppOperatorAction,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppWebhookEvent,
)
from app.core.whatsapp_phase12c import build_offline_drill
from app.services import whatsapp_dead_letters, whatsapp_queue


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def dead_letter_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        Client.__table__,
        Lead.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppOperationalControl.__table__,
        WhatsAppOperatorAction.__table__,
        WhatsAppOutboundIntent.__table__,
        WhatsAppOptOut.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    now = datetime.now(timezone.utc)
    correlations = {1: str(uuid4()), 2: str(uuid4())}
    with factory() as session:
        session.add_all([
            Client(id=1, name="Tenant One", is_active=True),
            Client(id=2, name="Tenant Two", is_active=True),
            Lead(id=1, client_id=1, phone="15550000001", status="Contacted"),
            Lead(id=2, client_id=2, phone="15550000002", status="Contacted"),
            WhatsAppOperationalControl(id=1, control_key="global:global_outbound", scope="global", control_type="global_outbound", enabled=True, version=1, updated_by="bootstrap", reason="bootstrap", correlation_id=str(uuid4())),
            WhatsAppOperationalControl(id=2, control_key="global:worker_consumption", scope="global", control_type="worker_consumption", enabled=True, version=1, updated_by="bootstrap", reason="bootstrap", correlation_id=str(uuid4())),
            WhatsAppWebhookEvent(id=1, client_id=1, event_kind="message", event_id="dead-tenant-1", correlation_id=correlations[1], phone_number_id="phone-1", payload={"id": "dead-tenant-1", "from": "15550000001", "text": {"body": "synthetic only"}}, state="dead_letter", attempt_count=3, last_error="ValueError: synthetic failure", received_at=now - timedelta(minutes=5), dead_lettered_at=now - timedelta(minutes=1)),
            WhatsAppWebhookEvent(id=2, client_id=2, event_kind="message", event_id="dead-tenant-2", correlation_id=correlations[2], phone_number_id="phone-2", payload={"id": "dead-tenant-2", "from": "15550000002"}, state="dead_letter", attempt_count=1, last_error="RuntimeError: synthetic failure", received_at=now - timedelta(minutes=4), dead_lettered_at=now),
        ])
        session.commit()
    yield factory, correlations
    engine.dispose()


def test_dead_letter_listing_is_bounded_redacted_and_tenant_scoped(dead_letter_db):
    _factory, correlations = dead_letter_db
    result = whatsapp_dead_letters.list_dead_letters(client_id=1, limit=1)

    assert result["limit"] == 1
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["receipt_id"] == 1
    assert item["correlation_id"] == correlations[1]
    assert item["error_type"] == "ValueError"
    assert item["replay_eligible"] is True
    assert "payload" not in item and "phone" not in item and "last_error" not in item


def test_replay_is_audited_idempotent_and_preserves_original_evidence(dead_letter_db, monkeypatch):
    factory, correlations = dead_letter_db
    calls: list[dict[str, object]] = []

    def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return kwargs["job_id"]

    monkeypatch.setattr(whatsapp_queue, "enqueue_persisted_receipt", fake_enqueue)

    def replay():
        return whatsapp_dead_letters.replay_dead_letters(
            client_id=1,
            items=[(1, correlations[1])],
            replay_limit=1,
            actor="tenant:1:authenticated-session",
            reason="synthetic recovery",
        )

    first = replay()
    second = replay()

    assert first["outcomes"][0]["state"] == "queued"
    assert second["outcomes"][0] == {"receipt_id": 1, "correlation_id": correlations[1], "state": "already_queued", "idempotent": True}
    assert len(calls) == 1
    with factory() as session:
        receipt = session.get(WhatsAppWebhookEvent, 1)
        actions = session.query(WhatsAppOperatorAction).all()
        assert receipt.correlation_id == correlations[1]
        assert receipt.dead_lettered_at is not None
        assert receipt.last_error == "ValueError: synthetic failure"
        assert receipt.payload["text"]["body"] == "synthetic only"
        assert len(actions) == 1
        assert actions[0].operator_id == "tenant:1:authenticated-session"
        assert actions[0].reason == "synthetic recovery"
        assert actions[0].correlation_id == correlations[1]


def test_replay_denies_cross_tenant_correlation_and_unbounded_requests(dead_letter_db):
    _factory, correlations = dead_letter_db
    with pytest.raises(whatsapp_dead_letters.DeadLetterConflict):
        whatsapp_dead_letters.replay_dead_letters(client_id=1, items=[(2, correlations[2])], replay_limit=1, actor="tenant:1:authenticated-session", reason="cross tenant")
    with pytest.raises(whatsapp_dead_letters.DeadLetterError, match="bounded"):
        whatsapp_dead_letters.replay_dead_letters(client_id=1, items=[(1, correlations[1]), (2, correlations[2])], replay_limit=1, actor="tenant:1:authenticated-session", reason="too many")


def test_replay_rechecks_kill_switch_and_opt_out_policy(dead_letter_db, monkeypatch):
    factory, correlations = dead_letter_db
    monkeypatch.setattr(whatsapp_queue, "enqueue_persisted_receipt", lambda **_kwargs: pytest.fail("blocked replay must not enqueue"))
    with factory() as session:
        session.add(WhatsAppOperationalControl(control_key="tenant:1:tenant_outbound", scope="tenant", client_id=1, control_type="tenant_outbound", enabled=False, version=1, updated_by="operator", reason="incident", correlation_id=str(uuid4())))
        session.commit()
    with pytest.raises(whatsapp_dead_letters.DeadLetterConflict, match="controls"):
        whatsapp_dead_letters.replay_dead_letters(client_id=1, items=[(1, correlations[1])], replay_limit=1, actor="tenant:1:authenticated-session", reason="blocked")

    with factory() as session:
        session.query(WhatsAppOperationalControl).filter_by(control_key="tenant:1:tenant_outbound").one().enabled = True
        session.add(WhatsAppOptOut(client_id=1, phone="15550000001", opted_out_at=datetime.now(timezone.utc), reason="synthetic opt out", source="test", policy_version="phase7-v1"))
        session.commit()
    with pytest.raises(whatsapp_dead_letters.DeadLetterConflict, match="policy"):
        whatsapp_dead_letters.replay_dead_letters(client_id=1, items=[(1, correlations[1])], replay_limit=1, actor="tenant:1:authenticated-session", reason="blocked")


def test_replay_router_uses_authenticated_tenant_and_bounded_models():
    api_routes = [route for route in routes.router.routes if isinstance(route, APIRoute)]
    assert {route.path for route in api_routes} == {"/api/whatsapp-operations/dead-letters", "/api/whatsapp-operations/dead-letters/replay"}
    assert all(route.response_model is not None for route in api_routes)
    assert all(routes.require_api_key in {dependency.call for dependency in route.dependant.dependencies} for route in api_routes)


@pytest.mark.parametrize("scenario", ["worker_pause_resume", "queue_backlog_alert", "dead_letter_replay", "kill_switch_activation", "alert_cooldown", "correlation_lookup", "rollback_decision"])
def test_operational_drills_are_provider_disabled(scenario):
    drill = build_offline_drill(scenario)
    assert drill["mode"] == "offline"
    assert drill["provider_calls_enabled"] is False
    assert drill["production_mutations_enabled"] is False
    assert drill["steps"]


def test_deterministic_replay_enqueue_recovers_concurrent_existing_job(dead_letter_db, monkeypatch):
    factory, correlations = dead_letter_db
    with factory() as session:
        receipt = session.get(WhatsAppWebhookEvent, 1)
        receipt.state = "replay_requested"
        receipt.rq_job_id = "whatsapp-replay-1-3"
        session.commit()

    class Queue:
        fetches = 0

        def fetch_job(self, job_id):
            self.fetches += 1
            return None if self.fetches == 1 else type("Job", (), {"id": job_id})()

        def enqueue(self, *_args, **_kwargs):
            raise RuntimeError("concurrent job id")

    monkeypatch.setattr(whatsapp_queue, "webhook_queue", Queue())
    job_id = whatsapp_queue.enqueue_persisted_receipt(
        receipt_id=1,
        client_id=1,
        job_id="whatsapp-replay-1-3",
    )

    assert job_id == "whatsapp-replay-1-3"
    with factory() as session:
        receipt = session.get(WhatsAppWebhookEvent, 1)
        assert receipt.correlation_id == correlations[1]
        assert receipt.dead_lettered_at is not None
        assert receipt.last_error == "ValueError: synthetic failure"

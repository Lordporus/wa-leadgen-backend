from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import database
from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    Message,
    WhatsAppAIDecisionAudit,
    WhatsAppAIPromptModel,
    WhatsAppOperatorAction,
    WhatsAppOutboundIntent,
    WhatsAppTakeoverTask,
    WhatsAppWebhookEvent,
)
from app.services import whatsapp_inbox, whatsapp_outbox
from app.store import db_client


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def inbox_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Client.__table__,
        Lead.__table__,
        WhatsAppAIPromptModel.__table__,
        WhatsAppAIDecisionAudit.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppOperatorAction.__table__,
        WhatsAppTakeoverTask.__table__,
        WhatsAppOutboundIntent.__table__,
        Message.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory() as session:
        session.add_all([
            Client(id=1, name="Tenant One", is_active=True),
            Client(id=2, name="Tenant Two", is_active=True),
            Lead(id=1, client_id=1, phone="15550000001", status="Contacted"),
            Lead(id=2, client_id=2, phone="15550000002", status="Contacted"),
        ])
        session.commit()
    yield factory
    engine.dispose()


def _ai_intent(factory, *, suffix: str, version: int = 0) -> int:
    with factory() as session:
        event = WhatsAppWebhookEvent(
            client_id=1,
            event_kind="message",
            event_id=f"event-{suffix}",
            correlation_id=f"corr-{suffix}",
            phone_number_id="phone-1",
            payload={},
            state="processed",
        )
        session.add(event)
        session.flush()
        intent = WhatsAppOutboundIntent(
            client_id=1,
            inbound_event_id=event.id,
            recipient_phone="15550000001",
            body="Persisted AI reply",
            state="generating",
            intent_kind="ai_reply",
            takeover_version=version,
            correlation_id=f"corr-{suffix}",
        )
        session.add(intent)
        session.flush()
        session.add(Message(
            lead_id=1,
            direction="OUTBOUND",
            msg_type="text",
            body=intent.body,
            status="pending",
            outbound_intent_id=intent.id,
        ))
        session.commit()
        return intent.id


def _takeover(
    *,
    reason: str = "customer_requested_human",
    operator_id: str = "operator-1",
) -> whatsapp_inbox.TakeoverState:
    return whatsapp_inbox.transition_takeover(
        client_id=1,
        lead_id=1,
        enabled=True,
        expected_version=0,
        operator_id=operator_id,
        reason=reason,
        correlation_id="corr-takeover",
        confirmed=True,
    )


def _allow_policy(monkeypatch):
    decision = SimpleNamespace(allowed=True, reason_code="allowed")
    monkeypatch.setattr(
        whatsapp_outbox.whatsapp_policy,
        "tenant_meta_credentials",
        lambda _client: SimpleNamespace(),
    )
    monkeypatch.setattr(
        whatsapp_outbox.whatsapp_policy,
        "evaluate_locked",
        lambda *_args, **_kwargs: decision,
    )
    monkeypatch.setattr(
        whatsapp_outbox.whatsapp_policy,
        "set_provider_audit_outcome",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        whatsapp_outbox.whatsapp_policy,
        "persist_policy_decision",
        lambda *_args, **_kwargs: None,
    )


def test_takeover_is_versioned_audited_and_blocks_pending_ai(inbox_db):
    intent_id = _ai_intent(inbox_db, suffix="takeover")

    state = _takeover()

    assert state.enabled is True
    assert state.version == 1
    with inbox_db() as session:
        lead = session.get(Lead, 1)
        intent = session.get(WhatsAppOutboundIntent, intent_id)
        message = session.query(Message).filter_by(outbound_intent_id=intent_id).one()
        action = session.query(WhatsAppOperatorAction).filter_by(action="takeover").one()
        task = session.query(WhatsAppTakeoverTask).one()
        assert lead is not None and lead.is_human_takeover is True
        assert intent is not None and intent.state == "blocked"
        assert message.status == "blocked"
        assert action.from_version == 0 and action.to_version == 1
        assert task.status == "open" and task.takeover_version == 1

    with pytest.raises(whatsapp_inbox.InboxConflict, match="stale_takeover_version"):
        _takeover()


def test_release_requires_confirmation_and_never_revives_stale_ai(inbox_db, monkeypatch):
    intent_id = _ai_intent(inbox_db, suffix="release")
    _takeover(reason="ai_escalation", operator_id="system:ai-escalation")
    with pytest.raises(whatsapp_inbox.InboxConflict, match="release_confirmation_required"):
        whatsapp_inbox.transition_takeover(
            client_id=1, lead_id=1, enabled=False, expected_version=1,
            operator_id="operator-1", reason="release", correlation_id="corr-release",
            confirmed=False,
        )
    state = whatsapp_inbox.transition_takeover(
        client_id=1, lead_id=1, enabled=False, expected_version=1,
        operator_id="operator-1", reason="release", correlation_id="corr-release",
        confirmed=True,
    )
    assert state.version == 2 and state.enabled is False
    with inbox_db() as session:
        task = session.query(WhatsAppTakeoverTask).one()
        actions = session.query(WhatsAppOperatorAction).order_by(
            WhatsAppOperatorAction.id
        ).all()
        assert task.status == "resolved"
        assert [action.action for action in actions] == ["takeover", "release"]

    # Reproduce a stale/replayed worker restoring its old claim after release.
    with inbox_db() as session:
        intent = session.get(WhatsAppOutboundIntent, intent_id)
        assert intent is not None
        intent.state = "generating"
        session.commit()
    _allow_policy(monkeypatch)
    sends: list[str] = []

    def unsafe_sender(*_args, **_kwargs):
        sends.append("sent")
        return "wamid-unsafe"

    result = whatsapp_outbox.dispatch_intent(
        intent_id=intent_id,
        client_id=1,
        sender=unsafe_sender,
    )
    assert result.state == "blocked"
    assert sends == []
    with inbox_db() as session:
        intent = session.get(WhatsAppOutboundIntent, intent_id)
        assert intent is not None
        assert intent.failure_category == "stale_takeover_version"


def test_manual_send_uses_one_intent_and_one_operator_audit_on_retry(inbox_db, monkeypatch):
    _takeover()
    intent_id, created = whatsapp_inbox.create_manual_intent(
        client_id=1, lead_id=1, body="Approved manual response",
        idempotency_key="manual-key-000001", operator_id="operator-1",
        correlation_id="corr-manual",
    )
    retry_id, retry_created = whatsapp_inbox.create_manual_intent(
        client_id=1, lead_id=1, body="Approved manual response",
        idempotency_key="manual-key-000001", operator_id="operator-1",
        correlation_id="corr-retry",
    )
    assert (intent_id, created) == (retry_id, True)
    assert retry_created is False

    _allow_policy(monkeypatch)
    sends: list[str] = []

    def first_sender(*_args, **_kwargs):
        sends.append("send")
        return "wamid-manual-1"

    def duplicate_sender(*_args, **_kwargs):
        sends.append("duplicate")
        return "wamid-duplicate"

    first = whatsapp_outbox.dispatch_intent(
        intent_id=intent_id,
        client_id=1,
        sender=first_sender,
        action="human_manual_send",
        allow_human_takeover=True,
    )
    second = whatsapp_outbox.dispatch_intent(
        intent_id=intent_id,
        client_id=1,
        sender=duplicate_sender,
        action="human_manual_send",
        allow_human_takeover=True,
    )
    assert first.newly_sent is True and second.newly_sent is False
    assert sends == ["send"]
    monkeypatch.setattr(db_client, "SessionLocal", inbox_db)
    postgres_store = db_client.DatabaseClient()
    postgres_store.ok = True
    assert postgres_store.append_message(
        "15550000001", "outbound", "Approved manual response", "human",
        "wamid-manual-1", client_id=1,
    )
    with inbox_db() as session:
        assert session.query(WhatsAppOutboundIntent).filter_by(intent_kind="manual").count() == 1
        action = session.query(WhatsAppOperatorAction).filter_by(action="manual_send").one()
        assert action.outcome == "sent" and action.outbound_intent_id == intent_id
        message = session.query(Message).filter_by(outbound_intent_id=intent_id).one()
        assert message.status == "sent" and message.msg_type == "human"
        assert session.query(Message).filter_by(wa_message_id="wamid-manual-1").count() == 1

    row = next(item for item in whatsapp_inbox.timeline(client_id=1, lead_id=1) if item["id"] == f"m{message.id}")
    assert row["send_state"] == "sent"
    assert row["provider_status"] == "sent"
    assert row["role"] == "human"


def test_manual_failure_is_visible_and_operator_audit_terminal(inbox_db, monkeypatch):
    _takeover()
    intent_id, _ = whatsapp_inbox.create_manual_intent(
        client_id=1, lead_id=1, body="Manual failure",
        idempotency_key="manual-key-000002", operator_id="operator-1",
        correlation_id="corr-failure",
    )
    _allow_policy(monkeypatch)

    def fail(*_args, **_kwargs):
        raise ValueError("offline provider rejection")

    with pytest.raises(ValueError, match="offline provider rejection"):
        whatsapp_outbox.dispatch_intent(
            intent_id=intent_id, client_id=1, sender=fail,
            action="human_manual_send", allow_human_takeover=True,
        )
    row = next(item for item in whatsapp_inbox.timeline(client_id=1, lead_id=1) if item["send_state"] == "failed")
    assert row["failure_category"] == "ValueError"
    assert row["correlation_id"] == "corr-failure"
    with inbox_db() as session:
        action = session.query(WhatsAppOperatorAction).filter_by(action="manual_send").one()
        assert action.outcome == "failed"


def test_timeline_is_chronological_and_tenant_scoped(inbox_db):
    now = datetime.utcnow()
    with inbox_db() as session:
        session.add_all([
            Message(lead_id=1, direction="OUTBOUND", msg_type="text", body="Later", status="sent", created_at=now),
            Message(lead_id=1, direction="INBOUND", msg_type="text", body="Earlier", status="received", created_at=now - timedelta(minutes=1)),
            Message(lead_id=2, direction="INBOUND", msg_type="text", body="Tenant two secret", status="received", created_at=now),
        ])
        session.commit()
    rows = whatsapp_inbox.timeline(client_id=1, lead_id=1)
    assert [row["content"] for row in rows] == ["Earlier", "Later"]
    assert all("Tenant two secret" not in row["content"] for row in rows)
    with pytest.raises(whatsapp_inbox.InboxConflict, match="lead_not_found"):
        whatsapp_inbox.timeline(client_id=2, lead_id=1)


def test_takeover_queue_acknowledge_resolve_and_tenant_isolation(inbox_db):
    _takeover()
    task = whatsapp_inbox.list_tasks(client_id=1)[0]
    assert whatsapp_inbox.list_tasks(client_id=2) == []
    with pytest.raises(whatsapp_inbox.InboxConflict, match="task_not_found"):
        whatsapp_inbox.update_task(
            client_id=2, task_id=task["id"], operator_id="operator-2",
            resolve=False, correlation_id="corr-wrong-tenant",
        )
    acknowledged = whatsapp_inbox.update_task(
        client_id=1, task_id=task["id"], operator_id="operator-1",
        resolve=False, correlation_id="corr-ack",
    )
    assert acknowledged["status"] == "acknowledged"
    resolved = whatsapp_inbox.update_task(
        client_id=1, task_id=task["id"], operator_id="operator-1",
        resolve=True, correlation_id="corr-resolve",
    )
    assert resolved["status"] == "resolved"
    assert whatsapp_inbox.list_tasks(client_id=1) == []
    with inbox_db() as session:
        action_count = session.query(WhatsAppOperatorAction).count()
    with pytest.raises(whatsapp_inbox.InboxConflict, match="task_already_resolved"):
        whatsapp_inbox.update_task(
            client_id=1, task_id=task["id"], operator_id="replay-operator",
            resolve=False, correlation_id="corr-delayed-ack",
        )
    replayed = whatsapp_inbox.update_task(
        client_id=1, task_id=task["id"], operator_id="replay-operator",
        resolve=True, correlation_id="corr-replayed-resolve",
    )
    assert replayed["status"] == "resolved"
    assert replayed["owner"] == "operator-1"
    with inbox_db() as session:
        assert session.get(WhatsAppTakeoverTask, task["id"]).status == "resolved"
        assert session.query(WhatsAppOperatorAction).count() == action_count


def test_operator_action_history_is_tenant_scoped(inbox_db):
    _takeover(operator_id="trusted-operator")

    rows = whatsapp_inbox.list_operator_actions(client_id=1, lead_id=1)

    assert rows[0]["operator_id"] == "trusted-operator"
    assert rows[0]["action"] == "takeover"
    assert rows[0]["correlation_id"] == "corr-takeover"
    with pytest.raises(whatsapp_inbox.InboxConflict, match="lead_not_found"):
        whatsapp_inbox.list_operator_actions(client_id=2, lead_id=1)

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
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
    WhatsAppConsentRecord,
    WhatsAppOperationalControl,
    WhatsAppOperationalControlAudit,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppPolicyDecision,
    WhatsAppSequence,
    WhatsAppSequenceEnrollment,
    WhatsAppSequenceExecution,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
    WhatsAppWebhookEvent,
)
from app.services import (
    jobs,
    whatsapp_operations,
    whatsapp_outbox,
    whatsapp_policy,
    whatsapp_queue,
    whatsapp_sequences,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def operations_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Client.__table__,
        Lead.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppOutboundIntent.__table__,
        Message.__table__,
        WhatsAppConsentRecord.__table__,
        WhatsAppOptOut.__table__,
        WhatsAppTenantPolicy.__table__,
        WhatsAppTemplate.__table__,
        WhatsAppPolicyDecision.__table__,
        WhatsAppSequence.__table__,
        WhatsAppSequenceEnrollment.__table__,
        WhatsAppSequenceExecution.__table__,
        WhatsAppOperationalControl.__table__,
        WhatsAppOperationalControlAudit.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    now = datetime.now(timezone.utc)
    with factory() as session:
        for client_id in (1, 2):
            session.add(
                Client(
                    id=client_id,
                    name=f"Tenant {client_id}",
                    is_active=True,
                    wa_phone_number_id=f"phone-{client_id}",
                    wa_business_account_id=f"waba-{client_id}",
                    wa_access_token_env_var="WHATSAPP_TEST_TENANT_TOKEN",
                )
            )
            session.add(
                Lead(
                    id=client_id,
                    client_id=client_id,
                    phone=f"1555000000{client_id}",
                    status="Contacted",
                )
            )
            session.add(
                WhatsAppTenantPolicy(
                    client_id=client_id,
                    timezone="UTC",
                    max_messages_per_window=100,
                    daily_cap=100,
                    excluded_lead_stages=["Booked", "Lost"],
                )
            )
            session.add(
                WhatsAppConsentRecord(
                    client_id=client_id,
                    phone=f"1555000000{client_id}",
                    source="test",
                    consented_at=now - timedelta(days=1),
                    policy_version="phase7-v1",
                )
            )
            session.add(
                Message(
                    lead_id=client_id,
                    direction="INBOUND",
                    msg_type="text",
                    body="hello",
                    channel="whatsapp",
                    created_at=now,
                )
            )
        session.flush()
        template_1 = WhatsAppTemplate(
            id=1,
            client_id=1,
            name="approved_one",
            language="en",
            category="utility",
            variables=[],
            version="v1",
            approval_status="approved",
            meta_status="approved",
            verified_at=now,
            verified_waba_id="waba-1",
            verified_phone_number_id="phone-1",
            component_signature=[],
        )
        template_2 = WhatsAppTemplate(
            id=2,
            client_id=2,
            name="approved_two",
            language="en",
            category="utility",
            variables=[],
            version="v1",
            approval_status="approved",
            meta_status="approved",
            verified_at=now,
            verified_waba_id="waba-2",
            verified_phone_number_id="phone-2",
            component_signature=[],
        )
        session.add_all([template_1, template_2])
        session.add_all(
            [
                WhatsAppSequence(
                    id=1, client_id=1, name="Sequence 1", status="active"
                ),
                WhatsAppSequence(
                    id=2, client_id=2, name="Sequence 2", status="active"
                ),
            ]
        )
        for control_key, control_type, correlation_id in (
            (
                "global:global_outbound",
                whatsapp_operations.GLOBAL_OUTBOUND,
                "00000000-0000-4000-8000-000000000022",
            ),
            (
                "global:worker_consumption",
                whatsapp_operations.WORKER_CONSUMPTION,
                "00000000-0000-4000-8000-000000000023",
            ),
        ):
            control = WhatsAppOperationalControl(
                control_key=control_key,
                scope="global",
                client_id=None,
                control_type=control_type,
                resource_id=None,
                enabled=True,
                version=1,
                updated_by="system:migration:0022",
                reason="phase12a_bootstrap_enabled",
                correlation_id=correlation_id,
            )
            session.add(control)
            session.flush()
            session.add(
                WhatsAppOperationalControlAudit(
                    control_id=control.id,
                    control_key=control_key,
                    scope="global",
                    client_id=None,
                    control_type=control_type,
                    resource_id=None,
                    from_enabled=None,
                    to_enabled=True,
                    from_version=0,
                    to_version=1,
                    operator_id="system:migration:0022",
                    reason="phase12a_bootstrap_enabled",
                    correlation_id=correlation_id,
                )
            )
        session.commit()
    yield factory
    engine.dispose()


def _transition(
    *,
    control,
    enabled,
    correlation,
    client_id=None,
    resource_id=None,
    expected_version=None,
):
    if expected_version is None:
        expected_version = (
            1 if control in whatsapp_operations.GLOBAL_CONTROLS else 0
        )
    return whatsapp_operations.mutate(
        control=control,
        enabled_value=enabled,
        expected_version=expected_version,
        operator_id="tenant:1:authenticated-session",
        reason="offline control test",
        correlation_id=correlation,
        client_id=client_id,
        resource_id=resource_id,
    )


def test_transition_is_atomic_versioned_audited_and_idempotent(operations_db):
    correlation = str(uuid4())
    first = _transition(
        control=whatsapp_operations.TENANT_OUTBOUND,
        enabled=False,
        correlation=correlation,
        client_id=1,
    )
    replay = _transition(
        control=whatsapp_operations.TENANT_OUTBOUND,
        enabled=False,
        correlation=correlation,
        client_id=1,
    )

    assert first.version == replay.version == 1
    assert first.effective_enabled is False
    with operations_db() as session:
        assert session.query(WhatsAppOperationalControl).filter_by(
            control_key="tenant:1:tenant_outbound"
        ).count() == 1
        audit = session.query(WhatsAppOperationalControlAudit).filter_by(
            correlation_id=correlation
        ).one()
        assert (audit.from_version, audit.to_version) == (0, 1)
        assert audit.operator_id == "tenant:1:authenticated-session"
        assert audit.reason == "offline control test"

    with pytest.raises(
        whatsapp_operations.OperationalControlConflict,
        match="stale control version",
    ):
        _transition(
            control=whatsapp_operations.TENANT_OUTBOUND,
            enabled=True,
            correlation=str(uuid4()),
            client_id=1,
            expected_version=0,
        )

def test_resource_controls_reject_cross_tenant_targets(operations_db):
    with pytest.raises(
        whatsapp_operations.OperationalControlError,
        match="does not belong",
    ):
        _transition(
            control=whatsapp_operations.TEMPLATE,
            enabled=False,
            correlation=str(uuid4()),
            client_id=1,
            resource_id=2,
        )
    with pytest.raises(
        whatsapp_operations.OperationalControlError,
        match="does not belong",
    ):
        _transition(
            control=whatsapp_operations.SEQUENCE,
            enabled=False,
            correlation=str(uuid4()),
            client_id=1,
            resource_id=2,
        )
    assert whatsapp_operations.list_states(client_id=1) == [
        {
            "control": "ai_auto_reply",
            "enabled": True,
            "effective_enabled": True,
            "version": 0,
            "scope": "tenant",
            "client_id": 1,
            "resource_id": None,
            "updated_by": None,
            "reason": None,
            "correlation_id": None,
            "updated_at": None,
        },
        {
            "control": "tenant_outbound",
            "enabled": True,
            "effective_enabled": True,
            "version": 0,
            "scope": "tenant",
            "client_id": 1,
            "resource_id": None,
            "updated_by": None,
            "reason": None,
            "correlation_id": None,
            "updated_at": None,
        },
    ]


@pytest.mark.parametrize(
    ("control", "client_id", "reason"),
    [
        (
            whatsapp_operations.GLOBAL_OUTBOUND,
            None,
            "global_operational_control",
        ),
        (
            whatsapp_operations.TENANT_OUTBOUND,
            1,
            "tenant_operational_control",
        ),
    ],
)
def test_outbound_controls_block_locked_policy(
    operations_db, control, client_id, reason
):
    _transition(
        control=control,
        enabled=False,
        correlation=str(uuid4()),
        client_id=client_id,
    )
    with operations_db() as session:
        client = session.query(Client).filter_by(id=1).one()
        lead = session.query(Lead).filter_by(id=1, client_id=1).one()
        decision = whatsapp_policy.evaluate_locked(
            session,
            client=client,
            lead=lead,
            action="phase12a_test",
            message_type="text",
        )
        assert decision.allowed is False
        assert decision.reason_code == reason


def test_template_disable_is_tenant_scoped_and_checked_at_send_boundary(
    operations_db,
):
    _transition(
        control=whatsapp_operations.TEMPLATE,
        enabled=False,
        correlation=str(uuid4()),
        client_id=1,
        resource_id=1,
    )
    with operations_db() as session:
        client = session.query(Client).filter_by(id=1).one()
        lead = session.query(Lead).filter_by(id=1, client_id=1).one()
        template = session.query(WhatsAppTemplate).filter_by(
            id=1, client_id=1
        ).one()
        decision = whatsapp_policy.evaluate_locked(
            session,
            client=client,
            lead=lead,
            action="phase12a_template_test",
            message_type="template",
            template=template,
        )
        assert decision.allowed is False
        assert decision.reason_code == "template_disabled"

        other = session.query(WhatsAppOperationalControl).filter_by(
            client_id=2, control_type=whatsapp_operations.TEMPLATE
        ).one_or_none()
        assert other is None


def test_ai_control_is_rechecked_before_final_outbox_send(operations_db):
    with operations_db() as session:
        event = WhatsAppWebhookEvent(
            client_id=1,
            event_kind="message",
            event_id="phase12a-ai-final",
            correlation_id=str(uuid4()),
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
            body="approved deterministic response",
            state="generating",
            correlation_id=event.correlation_id,
            intent_kind="ai_reply",
            takeover_version=0,
        )
        session.add(intent)
        session.commit()
        intent_id = intent.id

    _transition(
        control=whatsapp_operations.AI_AUTO_REPLY,
        enabled=False,
        correlation=str(uuid4()),
        client_id=1,
    )

    result = whatsapp_outbox.dispatch_intent(
        intent_id=intent_id,
        client_id=1,
        sender=lambda *_args, **_kwargs: pytest.fail(
            "disabled AI must never reach provider"
        ),
    )
    assert result.state == "blocked"
    with operations_db() as session:
        intent = session.get(WhatsAppOutboundIntent, intent_id)
        assert intent.state == "blocked"
        assert intent.failure_category == "ai_auto_reply_disabled"


def test_ai_disable_preserves_inbound_before_escalating(
    operations_db, monkeypatch
):
    _transition(
        control=whatsapp_operations.AI_AUTO_REPLY,
        enabled=False,
        correlation=str(uuid4()),
        client_id=1,
    )
    order = []

    class Store:
        def get_lead(self, *_args, **_kwargs):
            return {
                "id": "lead-1",
                "fields": {
                    "Status": "Contacted",
                    "is_human_takeover": False,
                },
            }

        def append_message(self, *_args, **_kwargs):
            order.append("inbound_persisted")
            return True

        def update_human_takeover_by_id(self, *_args, **_kwargs):
            order.append("takeover_mirrored")

    monkeypatch.setattr(jobs, "get_store", lambda: Store())
    monkeypatch.setattr(
        jobs.tenant,
        "resolve_context_by_phone_id",
        lambda _phone_id: SimpleNamespace(
            client=SimpleNamespace(id=1),
            gemini=object(),
            won_stages=[],
            lost_stages=[],
        ),
    )
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "record_inbound_opt_out",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        jobs.whatsapp_inbox,
        "transition_takeover",
        lambda **_kwargs: order.append("escalated"),
    )
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "preflight_text",
        lambda **_kwargs: pytest.fail("AI-disabled path must stop first"),
    )

    jobs.process_webhook_message(
        "phone-1",
        {
            "id": "phase12a-inbound",
            "from": "15550000001",
            "type": "text",
            "text": {"body": "Please help"},
        },
        current_client_id=1,
        inbound_event_id="phase12a-inbound",
        correlation_id=str(uuid4()),
    )
    assert order == [
        "inbound_persisted",
        "escalated",
        "takeover_mirrored",
    ]


def test_sequence_pause_preserves_enrollment_for_resume(operations_db):
    _transition(
        control=whatsapp_operations.SEQUENCE,
        enabled=False,
        correlation=str(uuid4()),
        client_id=1,
        resource_id=1,
    )
    now = datetime.now(timezone.utc)
    with operations_db() as session:
        enrollment = WhatsAppSequenceEnrollment(
            id=1,
            sequence_id=1,
            lead_id=1,
            client_id=1,
            status="active",
            current_step=0,
            next_run_at=now,
        )
        session.add(enrollment)
        session.commit()
        lead = session.query(Lead).filter_by(id=1, client_id=1).one()
        reason = whatsapp_sequences._final_sequence_guard(
            session,
            session.query(Client).filter_by(id=1).one(),
            lead,
            enrollment_id=1,
            sequence_id=1,
        )
        assert reason == "operational_sequence_paused"
        assert enrollment.status == "active"
        execution = WhatsAppSequenceExecution(
            enrollment_id=1,
            client_id=1,
            step_position=0,
            attempt_number=1,
            state="sending",
        )
        session.add(execution)
        session.commit()
        execution_id = execution.id

    assert whatsapp_sequences._process_claim(execution_id, now) == "paused"
    with operations_db() as session:
        execution = session.get(WhatsAppSequenceExecution, execution_id)
        enrollment = session.get(WhatsAppSequenceEnrollment, 1)
        assert execution.state == "paused"
        assert enrollment.status == "active"
        assert enrollment.next_run_at is not None

    _transition(
        control=whatsapp_operations.SEQUENCE,
        enabled=True,
        correlation=str(uuid4()),
        client_id=1,
        resource_id=1,
        expected_version=1,
    )
    assert whatsapp_sequences._claim_due_enrollment(
        now + whatsapp_sequences._RETRY_DELAY + timedelta(seconds=1)
    ) == execution_id
    with operations_db() as session:
        assert session.get(
            WhatsAppSequenceExecution, execution_id
        ).state == "sending"
        assert session.query(WhatsAppSequenceExecution).count() == 1



def test_worker_pause_defers_without_processing_or_losing_receipt(monkeypatch):
    deferred = []

    class Queue:
        def enqueue_in(self, *args, **kwargs):
            deferred.append((args, kwargs))

    monkeypatch.setattr(
        whatsapp_queue.whatsapp_operations
        if hasattr(whatsapp_queue, "whatsapp_operations")
        else whatsapp_operations,
        "enabled",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        whatsapp_queue,
        "_restore_durable_correlation",
        lambda envelope: dict(envelope),
    )
    monkeypatch.setattr(whatsapp_queue, "webhook_queue", Queue())
    monkeypatch.setattr(
        whatsapp_queue,
        "_mark_state",
        lambda *_args, **_kwargs: pytest.fail(
            "paused receipt must remain queued"
        ),
    )
    whatsapp_queue.process_webhook_event(
        {
            "event_id": "phase12a-paused-worker",
            "event_kind": "message",
            "tenant_id": 1,
            "phone_number_id": "phone-1",
            "correlation_id": str(uuid4()),
            "attempt": 0,
            "payload": {"id": "phase12a-paused-worker"},
        }
    )
    assert len(deferred) == 1
    assert deferred[0][0][1] is whatsapp_queue.process_webhook_event


def test_migration_is_additive_and_deployment_keeps_migrations_separate():
    root = Path(__file__).parents[2]
    migration = (
        root
        / "alembic"
        / "versions"
        / "0022_add_whatsapp_operational_controls.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "0021"' in migration
    assert migration.count("op.create_table(") == 2
    assert "op.add_column(" not in migration
    assert "op.drop_" not in migration
    assert migration.count("phase12a_bootstrap_enabled") == 2
    assert migration.count("ON CONFLICT") == 2
    assert "global:global_outbound" in migration
    assert "global:worker_consumption" in migration

    deploy_sources = [
        root / "scripts" / "deploy-azure.sh",
        root / ".github" / "workflows" / "deploy-azure.yml",
        root / "deploy" / "docker-compose.production.yml",
    ]
    for source in deploy_sources:
        text = source.read_text(encoding="utf-8").lower()
        assert "alembic upgrade" not in text
        assert "run_migrations.py" not in text



def test_control_apis_are_protected_and_ignore_browser_operator_identity():
    from fastapi import Request, Response

    from app.api.dependencies import require_admin, require_api_key
    from app.api.routers import whatsapp_operations as routes
    from app.api.routers.admin import require_admin_secret

    tenant_put = next(
        route
        for route in routes.tenant_router.routes
        if isinstance(route, APIRoute)
        if route.path.endswith("/controls") and "PUT" in route.methods
    )
    admin_put = next(
        route
        for route in routes.admin_router.routes
        if isinstance(route, APIRoute)
        if route.path.endswith("/controls") and "PUT" in route.methods
    )
    assert require_api_key in {
        dependency.call for dependency in tenant_put.dependant.dependencies
    }
    assert {require_admin, require_admin_secret}.issubset(
        {dependency.call for dependency in admin_put.dependant.dependencies}
    )

    request_id = str(uuid4())
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/",
            "headers": [
                (b"x-request-id", request_id.encode()),
                (b"x-operator-id", b"spoofed-browser-operator"),
            ],
        }
    )
    response = Response()
    correlation_id, operator_id = routes._operation_context(
        request,
        response,
        actor="tenant:7:authenticated-session",
    )
    assert correlation_id == request_id
    assert operator_id == "tenant:7:authenticated-session"
    assert operator_id != "spoofed-browser-operator"

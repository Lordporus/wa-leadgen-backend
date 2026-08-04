from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core import database
from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    Message,
    WhatsAppConsentRecord,
    WhatsAppOptOut,
    WhatsAppOperationalControl,
    WhatsAppOperationalControlAudit,
    WhatsAppOutboundIntent,
    WhatsAppPolicyDecision,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
    WhatsAppWebhookEvent,
)
from app.services import whatsapp_operations, whatsapp_outbox, whatsapp_policy

pytestmark = pytest.mark.integration

_TABLES = [
    Client.__table__,
    Lead.__table__,
    WhatsAppWebhookEvent.__table__,
    WhatsAppOutboundIntent.__table__,
    Message.__table__,
    WhatsAppConsentRecord.__table__,
    WhatsAppOptOut.__table__,
    WhatsAppOperationalControl.__table__,
    WhatsAppOperationalControlAudit.__table__,
    WhatsAppTenantPolicy.__table__,
    WhatsAppTemplate.__table__,
    WhatsAppPolicyDecision.__table__,
]


@pytest.fixture
def postgres_policy_db(monkeypatch):
    database_url = os.getenv("PHASE7_TEST_DATABASE_URL", "")
    if not database_url:
        pytest.skip("PHASE7_TEST_DATABASE_URL is required for PostgreSQL races")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("PHASE7_TEST_DATABASE_URL must be PostgreSQL")

    schema = f"phase7_race_{uuid4().hex}"
    assert re.fullmatch(r"phase7_race_[a-f0-9]{32}", schema)
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        pool_size=8,
        max_overflow=0,
    )
    Base.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setenv(
        "WHATSAPP_POSTGRES_RACE_TENANT_TOKEN",
        "offline-placeholder-token",
    )
    _seed(factory)
    try:
        yield factory
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _seed(factory):
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add(
            Client(
                id=1,
                name="Tenant",
                is_active=True,
                wa_phone_number_id="phone-1",
                wa_business_account_id="waba-1",
                wa_access_token_env_var=(
                    "WHATSAPP_POSTGRES_RACE_TENANT_TOKEN"
                ),
            )
        )
        session.add(
            Lead(
                id=1,
                client_id=1,
                phone="15550000001",
                status="Contacted",
            )
        )
        session.add(
            WhatsAppTenantPolicy(
                client_id=1,
                timezone="UTC",
                max_messages_per_window=100,
                daily_cap=100,
                excluded_lead_stages=["Booked", "Lost"],
            )
        )
        session.add(
            WhatsAppConsentRecord(
                client_id=1,
                phone="15550000001",
                source="web_form",
                consented_at=now - timedelta(days=1),
                policy_version="phase7-v1",
            )
        )
        session.add(
            Message(
                lead_id=1,
                direction="INBOUND",
                msg_type="text",
                body="hello",
                channel="whatsapp",
                created_at=now,
            )
        )
        inbound = WhatsAppWebhookEvent(
            client_id=1,
            event_kind="message",
            event_id=f"wamid.in.{uuid4()}",
            correlation_id=str(uuid4()),
            phone_number_id="phone-1",
            payload={},
            state="processed",
        )
        session.add(inbound)
        session.flush()
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
        session.add(
            WhatsAppOutboundIntent(
                id=1,
                client_id=1,
                inbound_event_id=inbound.id,
                recipient_phone="15550000001",
                body="queued reply",
                state="generating",
                takeover_version=0,
            )
        )
        session.commit()


ProviderCall = tuple[tuple[Any, ...], dict[str, Any]]


def _record_opt_out(
    errors: list[Exception],
    completed: Event | None = None,
) -> None:
    try:
        whatsapp_policy.record_opt_out(
            client_id=1,
            phone="15550000001",
            reason="inbound_opt_out_intent",
            source="whatsapp_inbound",
        )
        if completed:
            completed.set()
    except Exception as exc:  # pragma: no cover - asserted below
        errors.append(exc)


def _dispatch(
    provider_calls: list[ProviderCall],
    results: list[whatsapp_outbox.DispatchResult],
    errors: list[Exception],
) -> None:
    def send_provider(*args: Any, **kwargs: Any) -> str:
        provider_calls.append((args, kwargs))
        return "wamid.out"

    try:
        results.append(
            whatsapp_outbox.dispatch_intent(
                intent_id=1,
                client_id=1,
                sender=send_provider,
            )
        )
    except Exception as exc:  # pragma: no cover - asserted below
        errors.append(exc)


def test_opt_out_commit_before_queued_send_claim_blocks_provider(
    postgres_policy_db,
):
    errors: list[Exception] = []
    opt_out_committed = Event()
    provider_calls: list[ProviderCall] = []
    dispatch_results: list[whatsapp_outbox.DispatchResult] = []

    opt_out_thread = Thread(
        target=_record_opt_out,
        args=(errors, opt_out_committed),
    )

    def dispatch_after_opt_out() -> None:
        if opt_out_committed.wait(5):
            _dispatch(provider_calls, dispatch_results, errors)

    dispatch_thread = Thread(target=dispatch_after_opt_out)
    opt_out_thread.start()
    dispatch_thread.start()
    opt_out_thread.join(10)
    dispatch_thread.join(10)

    assert not opt_out_thread.is_alive()
    assert not dispatch_thread.is_alive()
    assert errors == []
    assert provider_calls == []
    assert dispatch_results[0].state == "blocked"


def test_queued_send_waits_while_opt_out_transaction_is_committing(
    postgres_policy_db,
):
    factory = postgres_policy_db
    opt_out_flushed = Event()
    release_opt_out = Event()
    errors: list[Exception] = []
    provider_calls: list[ProviderCall] = []
    dispatch_results: list[whatsapp_outbox.DispatchResult] = []

    def pause_opt_out_after_flush(session, _context):
        if any(isinstance(row, WhatsAppOptOut) for row in session.new):
            opt_out_flushed.set()
            assert release_opt_out.wait(10)

    event.listen(factory.class_, "after_flush", pause_opt_out_after_flush)
    opt_out_thread = Thread(target=_record_opt_out, args=(errors,))
    dispatch_thread = Thread(
        target=_dispatch,
        args=(provider_calls, dispatch_results, errors),
    )
    try:
        opt_out_thread.start()
        assert opt_out_flushed.wait(5)
        dispatch_thread.start()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with factory() as session:
                state = session.get(WhatsAppOutboundIntent, 1).state
            if state == "sending":
                break
            time.sleep(0.01)
        assert state == "sending"
        assert provider_calls == []

        release_opt_out.set()
        opt_out_thread.join(10)
        dispatch_thread.join(10)
    finally:
        release_opt_out.set()
        event.remove(
            factory.class_,
            "after_flush",
            pause_opt_out_after_flush,
        )

    assert not opt_out_thread.is_alive()
    assert not dispatch_thread.is_alive()
    assert errors == []
    assert provider_calls == []
    assert dispatch_results[0].state == "blocked"
    with factory() as session:
        assert session.query(WhatsAppOptOut).count() == 1
        assert session.get(WhatsAppOutboundIntent, 1).state == "blocked"

def test_first_global_disable_serializes_with_final_send(
    postgres_policy_db,
):
    factory = postgres_policy_db
    disable_flushed = Event()
    release_disable = Event()
    disable_committed = Event()
    errors: list[Exception] = []
    provider_calls: list[ProviderCall] = []
    dispatch_results: list[whatsapp_outbox.DispatchResult] = []
    mutation_results: list[whatsapp_operations.ControlState] = []

    with factory() as session:
        initial = session.query(WhatsAppOperationalControl).filter_by(
            control_key="global:global_outbound"
        ).one()
        assert initial.enabled is True
        assert initial.version == 1
        assert session.query(WhatsAppOperationalControlAudit).filter_by(
            control_id=initial.id
        ).count() == 1

    def pause_disable_after_flush(session, _context):
        disabling = any(
            isinstance(row, WhatsAppOperationalControl)
            and row.control_key == "global:global_outbound"
            and row.enabled is False
            for row in session.dirty
        )
        if disabling:
            disable_flushed.set()
            assert release_disable.wait(10)

    def disable_global_outbound() -> None:
        try:
            mutation_results.append(
                whatsapp_operations.mutate(
                    control=whatsapp_operations.GLOBAL_OUTBOUND,
                    enabled_value=False,
                    expected_version=1,
                    operator_id="admin:1:authenticated-session",
                    reason="postgres serialization regression",
                    correlation_id=str(uuid4()),
                )
            )
            disable_committed.set()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    event.listen(factory.class_, "after_flush", pause_disable_after_flush)
    disable_thread = Thread(target=disable_global_outbound)
    dispatch_thread = Thread(
        target=_dispatch,
        args=(provider_calls, dispatch_results, errors),
    )
    try:
        disable_thread.start()
        assert disable_flushed.wait(5)
        dispatch_thread.start()

        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            with factory() as session:
                state = session.get(WhatsAppOutboundIntent, 1).state
            if state == "sending":
                break
            time.sleep(0.01)
        assert state == "sending"
        assert provider_calls == []
        assert not disable_committed.is_set()

        release_disable.set()
        disable_thread.join(10)
        dispatch_thread.join(10)
    finally:
        release_disable.set()
        event.remove(factory.class_, "after_flush", pause_disable_after_flush)

    assert not disable_thread.is_alive()
    assert not dispatch_thread.is_alive()
    assert errors == []
    assert disable_committed.is_set()
    assert mutation_results[0].version == 2
    assert provider_calls == []
    assert dispatch_results[0].state == "blocked"
    with factory() as session:
        control = session.query(WhatsAppOperationalControl).filter_by(
            control_key="global:global_outbound"
        ).one()
        assert control.enabled is False
        assert control.version == 2
        assert session.query(WhatsAppOperationalControlAudit).filter_by(
            control_id=control.id
        ).count() == 2
        assert session.get(WhatsAppOutboundIntent, 1).state == "blocked"

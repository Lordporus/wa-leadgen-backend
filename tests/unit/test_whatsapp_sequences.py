import threading
from datetime import datetime, timedelta, timezone

import pytest
from redis.exceptions import LockError
from requests.exceptions import ConnectionError
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clients.whatsapp_client import MetaTransportError
from app.core import database
from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    Message,
    WhatsAppConsentRecord,
    WhatsAppOptOut,
    WhatsAppPolicyDecision,
    WhatsAppOperationalControl,
    WhatsAppSequence,
    WhatsAppSequenceEnrollment,
    WhatsAppSequenceExecution,
    WhatsAppSequenceStep,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
    WhatsAppWebhookEvent,
    WhatsAppOutboundIntent,
)
from app.services import whatsapp_sequences
from app.services.whatsapp_policy import ImmediateSendResult, ProviderOutcomeUncertain
from app.store import db_client


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def sequence_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    tables = [
        Client.__table__,
        Lead.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppOutboundIntent.__table__,
        WhatsAppOperationalControl.__table__,
        Message.__table__,
        WhatsAppConsentRecord.__table__,
        WhatsAppOptOut.__table__,
        WhatsAppTenantPolicy.__table__,
        WhatsAppTemplate.__table__,
        WhatsAppPolicyDecision.__table__,
        WhatsAppSequence.__table__,
        WhatsAppSequenceStep.__table__,
        WhatsAppSequenceEnrollment.__table__,
        WhatsAppSequenceExecution.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory() as session:
        session.add_all(
            [
                Client(
                    id=1,
                    name="Tenant",
                    is_active=True,
                    wa_phone_number_id="phone-1",
                    wa_business_account_id="waba-1",
                    wa_access_token_env_var="WHATSAPP_TEST_TENANT_TOKEN",
                ),
                Lead(id=1, client_id=1, phone="15550000001", status="Contacted"),
                WhatsAppTenantPolicy(
                    client_id=1,
                    timezone="UTC",
                    max_messages_per_window=100,
                    daily_cap=100,
                    excluded_lead_stages=["Booked", "Lost"],
                ),
                WhatsAppConsentRecord(
                    client_id=1,
                    phone="15550000001",
                    source="test",
                    consented_at=datetime.now(timezone.utc) - timedelta(days=1),
                    policy_version="phase7-v1",
                ),
            ]
        )
        session.commit()
    yield factory
    engine.dispose()


def _template(factory):
    now = datetime.now(timezone.utc)
    with factory() as session:
        row = WhatsAppTemplate(
            client_id=1,
            name="follow_up",
            language="en",
            category="utility",
            variables=[],
            version=str(now.timestamp()),
            approval_status="approved",
            meta_status="approved",
            meta_template_id="template-1",
            verification_reference="test",
            verified_at=now,
            verification_expires_at=now + timedelta(days=1),
            verified_waba_id="waba-1",
            verified_phone_number_id="phone-1",
            meta_variable_count=0,
            component_signature=[],
        )
        session.add(row)
        session.commit()
        return row.id


def _active_sequence(factory, lead_id=1):
    template_id = _template(factory)
    sequence = whatsapp_sequences.create_sequence(
        1, "Follow up", [{"template_id": template_id, "delay_seconds": 0}]
    )
    whatsapp_sequences.set_sequence_status(1, sequence["id"], "activate")
    return sequence["id"], whatsapp_sequences.enroll(1, sequence["id"], [lead_id])[
        "enrolled_ids"
    ][0]


def test_transitions_template_reference_and_terminal_no_reenroll(sequence_db):
    with pytest.raises(ValueError, match="steps are required"):
        whatsapp_sequences.create_sequence(1, "x", [])
    sequence_id, enrollment_id = _active_sequence(sequence_db)
    assert (
        whatsapp_sequences.set_enrollment_status(1, enrollment_id, "cancel")[
            "stop_reason"
        ]
        == "cancelled"
    )
    with pytest.raises(ValueError, match="Cannot resume"):
        whatsapp_sequences.set_enrollment_status(1, enrollment_id, "resume")
    assert (
        whatsapp_sequences.enroll(1, sequence_id, [1])["skipped"][0]["reason"]
        == "already_enrolled_or_terminal"
    )


def test_due_tick_exactly_once_and_dry_run_never_sends(sequence_db, monkeypatch):
    _, enrollment_id = _active_sequence(sequence_db)
    sent: list[dict[str, object]] = []

    def send_template(**kwargs) -> ImmediateSendResult:
        sent.append(kwargs)
        return ImmediateSendResult("sent", "allowed", "wamid-1")

    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy,
        "send_immediate_template",
        send_template,
    )
    assert whatsapp_sequences.dry_run(1, enrollment_id)["dry_run"] and not sent
    assert whatsapp_sequences.process_due_enrollments()["sent"] == 1
    assert whatsapp_sequences.process_due_enrollments()["sent"] == 0 and len(sent) == 1
    with sequence_db() as session:
        assert (
            session.get(WhatsAppSequenceEnrollment, enrollment_id).status == "completed"
        )


def test_duplicate_worker_claim_does_not_send_twice(sequence_db, monkeypatch):
    _, enrollment_id = _active_sequence(sequence_db)
    sent: list[dict[str, object]] = []

    def send_template(**kwargs) -> ImmediateSendResult:
        sent.append(kwargs)
        return ImmediateSendResult("sent", "allowed", "wamid-2")

    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy,
        "send_immediate_template",
        send_template,
    )
    now = datetime.now(timezone.utc)
    first = whatsapp_sequences._claim_due_enrollment(now)
    assert first is not None
    assert whatsapp_sequences._claim_due_enrollment(now) is None
    assert whatsapp_sequences._process_claim(first, now) == "sent"
    assert len(sent) == 1


def test_reply_committed_at_final_guard_blocks_provider(sequence_db, monkeypatch):
    _, enrollment_id = _active_sequence(sequence_db)
    provider_calls: list[int] = []

    def final_boundary(**kwargs):
        with sequence_db() as session:
            session.add(
                Message(
                    lead_id=1,
                    direction="INBOUND",
                    msg_type="text",
                    body="reply",
                    channel="whatsapp",
                )
            )
            session.commit()
        with sequence_db() as session:
            reason = kwargs["final_guard"](
                session, session.get(Client, 1), session.get(Lead, 1)
            )
            assert reason == "inbound_reply"
        return ImmediateSendResult("blocked", "inbound_reply", None)

    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy, "send_immediate_template", final_boundary
    )
    assert whatsapp_sequences.process_due_enrollments()["blocked"] == 1
    assert provider_calls == []
    with sequence_db() as session:
        assert (
            session.get(WhatsAppSequenceEnrollment, enrollment_id).stop_reason
            == "inbound_reply"
        )


def _configure_inbound_store(sequence_db, monkeypatch):
    monkeypatch.setattr(db_client, "SessionLocal", sequence_db)


def _serialized_lock_helper(monkeypatch, controlled_thread, acquired, proceed):
    boundary = threading.Lock()
    original = db_client.lock_tenant_lead

    def serialized(session, *, client_id, phone):
        boundary.acquire()

        def release(_session):
            if boundary.locked():
                boundary.release()

        event.listen(session, "after_commit", release, once=True)
        event.listen(session, "after_rollback", release, once=True)
        if threading.current_thread().name == controlled_thread:
            acquired.set()
            assert proceed.wait(timeout=2)
        return original(session, client_id=client_id, phone=phone)

    monkeypatch.setattr(db_client, "lock_tenant_lead", serialized)


def test_reply_commit_wins_shared_lock_and_blocks_provider(sequence_db, monkeypatch):
    sequence_id, enrollment_id = _active_sequence(sequence_db)
    provider_calls: list[int] = []
    reply_acquired = threading.Event()
    allow_reply_commit = threading.Event()
    _configure_inbound_store(sequence_db, monkeypatch)
    _serialized_lock_helper(
        monkeypatch,
        "reply-first",
        reply_acquired,
        allow_reply_commit,
    )
    store = db_client.DatabaseClient()
    store.ok = True
    outcomes: list[str | bool | None] = []
    with sequence_db() as session:
        enrollment = session.get(WhatsAppSequenceEnrollment, enrollment_id)
        lead = session.get(Lead, enrollment.lead_id)
        assert whatsapp_sequences._automatic_stop_reason(
            session, enrollment, lead
        ) is None

    def send_boundary():
        with sequence_db() as session:
            client, lead = db_client.lock_tenant_lead(
                session,
                client_id=1,
                phone="15550000001",
            )
            reason = whatsapp_sequences._final_sequence_guard(
                session,
                client,
                lead,
                enrollment_id=enrollment_id,
                sequence_id=sequence_id,
            )
            if reason is None:
                provider_calls.append(1)
            session.commit()
            outcomes.append(reason)

    def persist_reply() -> None:
        outcomes.append(
            store.append_message(
                "15550000001",
                "inbound",
                "reply",
                wa_message_id="wamid-reply-first",
                client_id=1,
            )
        )

    reply = threading.Thread(target=persist_reply, name="reply-first")
    reply.start()
    assert reply_acquired.wait(timeout=2)
    sequence = threading.Thread(
        target=send_boundary,
        name="sequence-after-reply",
    )
    sequence.start()
    allow_reply_commit.set()
    reply.join(timeout=2)
    sequence.join(timeout=2)

    assert not reply.is_alive() and not sequence.is_alive()
    assert outcomes[0] is True
    with sequence_db() as session:
        inbound = session.query(Message).filter_by(
            wa_message_id="wamid-reply-first"
        ).one()
        assert inbound.direction == "INBOUND"
        assert inbound.channel == "whatsapp"
    assert "inbound_reply" in outcomes
    assert provider_calls == []
    with sequence_db() as session:
        enrollment = session.get(WhatsAppSequenceEnrollment, enrollment_id)
        assert enrollment.status == "stopped"
        assert enrollment.stop_reason == "inbound_reply"


def test_sequence_send_lock_wins_before_reply_persistence(sequence_db, monkeypatch):
    sequence_id, enrollment_id = _active_sequence(sequence_db)
    provider_calls: list[int] = []
    sequence_acquired = threading.Event()
    allow_sequence_send = threading.Event()
    _configure_inbound_store(sequence_db, monkeypatch)
    _serialized_lock_helper(
        monkeypatch,
        "sequence-first",
        sequence_acquired,
        allow_sequence_send,
    )
    store = db_client.DatabaseClient()
    store.ok = True
    outcomes: list[str | bool | None] = []

    def send_boundary():
        with sequence_db() as session:
            client, lead = db_client.lock_tenant_lead(
                session,
                client_id=1,
                phone="15550000001",
            )
            reason = whatsapp_sequences._final_sequence_guard(
                session,
                client,
                lead,
                enrollment_id=enrollment_id,
                sequence_id=sequence_id,
            )
            if reason is None:
                provider_calls.append(1)
            session.commit()
            outcomes.append(reason)

    sequence = threading.Thread(
        target=send_boundary,
        name="sequence-first",
    )
    sequence.start()
    assert sequence_acquired.wait(timeout=2)
    def persist_reply() -> None:
        outcomes.append(
            store.append_message(
                "15550000001",
                "inbound",
                "reply",
                wa_message_id="wamid-sequence-first",
                client_id=1,
            )
        )

    reply = threading.Thread(target=persist_reply, name="reply-after-sequence")
    reply.start()
    allow_sequence_send.set()
    sequence.join(timeout=2)
    reply.join(timeout=2)

    assert not sequence.is_alive() and not reply.is_alive()
    assert provider_calls == [1]
    with sequence_db() as session:
        client, lead = db_client.lock_tenant_lead(
            session,
            client_id=1,
            phone="15550000001",
        )
        assert (
            whatsapp_sequences._final_sequence_guard(
                session,
                client,
                lead,
                enrollment_id=enrollment_id,
                sequence_id=sequence_id,
            )
            == "inbound_reply"
        )
        session.commit()
    assert provider_calls == [1]


def test_required_inbound_persistence_is_idempotent(sequence_db, monkeypatch):
    _configure_inbound_store(sequence_db, monkeypatch)
    store = db_client.DatabaseClient()
    store.ok = True

    for _ in range(2):
        assert store.persist_inbound_message_required(
            "15550000001",
            "reply",
            wa_message_id="wamid-required-idempotent",
            client_id=1,
        )

    with sequence_db() as session:
        assert (
            session.query(Message)
            .filter_by(wa_message_id="wamid-required-idempotent")
            .count()
            == 1
        )


def test_accepted_uncommitted_provider_outcome_never_retries(sequence_db, monkeypatch):
    _, enrollment_id = _active_sequence(sequence_db)
    calls: list[int] = []

    def uncertain(**_kwargs):
        calls.append(1)
        raise ProviderOutcomeUncertain("accepted but commit failed")

    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy, "send_immediate_template", uncertain
    )
    assert whatsapp_sequences.process_due_enrollments()["stopped"] == 1
    assert whatsapp_sequences.process_due_enrollments()["processed"] == 0
    assert calls == [1]
    with sequence_db() as session:
        row = session.get(WhatsAppSequenceEnrollment, enrollment_id)
        assert row.stop_reason == "provider_outcome_uncertain"


@pytest.mark.parametrize(
    "error",
    [
        MetaTransportError("accepted but no response"),
        ConnectionError("connection lost without response"),
    ],
)
def test_uncertain_transport_outcome_never_retries(
    sequence_db,
    monkeypatch,
    error,
):
    _, enrollment_id = _active_sequence(sequence_db)
    calls: list[int] = []

    def uncertain(**_kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy,
        "send_immediate_template",
        uncertain,
    )
    assert whatsapp_sequences.process_due_enrollments()["stopped"] == 1
    assert whatsapp_sequences.process_due_enrollments()["processed"] == 0
    assert calls == [1]
    with sequence_db() as session:
        execution = session.query(WhatsAppSequenceExecution).one()
        enrollment = session.get(WhatsAppSequenceEnrollment, enrollment_id)
        assert execution.state == "unknown"
        assert enrollment.status == "stopped"
        assert enrollment.stop_reason == "provider_outcome_uncertain"


def test_scheduler_lock_blocks_duplicate_tick_and_allows_stale_lock_recovery(
    monkeypatch,
):
    from app.api import runtime

    lock = type(
        "Lock",
        (),
        {"acquire": lambda self, **_kwargs: False, "release": lambda self: None},
    )()
    queue = type(
        "Queue",
        (),
        {
            "connection": type(
                "Connection", (), {"lock": lambda self, *_args, **_kwargs: lock}
            )(),
            "enqueue_in": lambda *_args, **_kwargs: pytest.fail(
                "duplicate tick must not reschedule"
            ),
        },
    )()
    monkeypatch.setattr(runtime, "webhook_queue", queue)
    assert whatsapp_sequences.run_sequence_tick_job()["skipped"] == "scheduler_locked"

    recovered_lock = type(
        "Lock",
        (),
        {
            "acquire": lambda self, **_kwargs: True,
            "owned": lambda self: True,
            "extend": lambda self, *_args, **_kwargs: True,
            "release": lambda self: None,
        },
    )()
    enqueued: list[dict[str, str]] = []
    queue.connection = type(
        "Connection", (), {"lock": lambda self, *_args, **_kwargs: recovered_lock}
    )()
    queue.enqueue_in = lambda *_args, **kwargs: enqueued.append(kwargs)
    monkeypatch.setattr(
        whatsapp_sequences,
        "process_due_enrollments",
        lambda **_kwargs: {"processed": 0},
    )
    assert whatsapp_sequences.run_sequence_tick_job() == {"processed": 0}
    assert enqueued == [{"job_id": "whatsapp-sequence-tick"}]


def test_scheduler_renews_lock_during_tick_longer_than_initial_ttl(monkeypatch):
    from app.api import runtime

    extended = threading.Event()

    class Lock:
        def acquire(self, **_kwargs):
            return True

        def owned(self):
            return True

        def extend(self, *_args, **_kwargs):
            extended.set()
            return True

        def release(self):
            return None

    lock = Lock()
    enqueued: list[dict[str, str]] = []
    queue = type(
        "Queue",
        (),
        {
            "connection": type(
                "Connection",
                (),
                {"lock": lambda self, *_args, **_kwargs: lock},
            )(),
            "enqueue_in": lambda *_args, **kwargs: enqueued.append(kwargs),
        },
    )()
    monkeypatch.setattr(runtime, "webhook_queue", queue)
    monkeypatch.setattr(
        whatsapp_sequences,
        "_SCHEDULER_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        whatsapp_sequences,
        "process_due_enrollments",
        lambda **_kwargs: extended.wait(timeout=1) and {"processed": 0},
    )

    assert whatsapp_sequences.run_sequence_tick_job() == {"processed": 0}
    assert extended.is_set()
    assert enqueued == [{"job_id": "whatsapp-sequence-tick"}]


def test_scheduler_lost_ownership_does_not_extend_release_or_reschedule(monkeypatch):
    from app.api import runtime

    ownership_checked = threading.Event()

    class Lock:
        def acquire(self, **_kwargs):
            return True

        def owned(self):
            ownership_checked.set()
            return False

        def extend(self, *_args, **_kwargs):
            pytest.fail("must not extend a lock owned by another worker")

        def release(self):
            raise LockError("lock ownership changed")

    lock = Lock()
    queue = type(
        "Queue",
        (),
        {
            "connection": type(
                "Connection",
                (),
                {"lock": lambda self, *_args, **_kwargs: lock},
            )(),
            "enqueue_in": lambda *_args, **_kwargs: pytest.fail(
                "lost owner must not reschedule"
            ),
        },
    )()
    monkeypatch.setattr(runtime, "webhook_queue", queue)
    monkeypatch.setattr(
        whatsapp_sequences,
        "_SCHEDULER_RENEW_INTERVAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        whatsapp_sequences,
        "process_due_enrollments",
        lambda **_kwargs: ownership_checked.wait(timeout=1) and {"processed": 0},
    )

    assert whatsapp_sequences.run_sequence_tick_job() == {
        "skipped": "scheduler_lock_lost"
    }


def test_scheduler_ownership_loss_stops_before_next_claim(monkeypatch):
    claims: list[int] = []
    ownership = iter([True, False])

    def claim(_now) -> int:
        claims.append(1)
        return 10

    monkeypatch.setattr(
        whatsapp_sequences,
        "_claim_due_enrollment",
        claim,
    )
    monkeypatch.setattr(
        whatsapp_sequences,
        "_process_claim",
        lambda _execution_id, _now: "sent",
    )

    result = whatsapp_sequences.process_due_enrollments(
        limit=2,
        should_continue=lambda: next(ownership),
    )

    assert claims == [1]
    assert result["processed"] == 1
    assert result["skipped"] == 1


@pytest.mark.parametrize(
    "reason, mutate",
    [
        (
            "inbound_reply",
            lambda s: s.add(
                Message(
                    lead_id=1,
                    direction="INBOUND",
                    msg_type="text",
                    body="reply",
                    channel="whatsapp",
                )
            ),
        ),
        (
            "opt_out",
            lambda s: s.add(
                WhatsAppOptOut(
                    client_id=1,
                    phone="15550000001",
                    opted_out_at=datetime.now(timezone.utc),
                    reason="test",
                    source="test",
                    policy_version="phase7-v1",
                )
            ),
        ),
        (
            "human_takeover",
            lambda s: setattr(s.get(Lead, 1), "is_human_takeover", True),
        ),
        ("booked", lambda s: setattr(s.get(Lead, 1), "status", "Booked")),
        ("lost", lambda s: setattr(s.get(Lead, 1), "status", "Lost")),
    ],
)
def test_automatic_stop_reasons(sequence_db, monkeypatch, reason, mutate):
    _, enrollment_id = _active_sequence(sequence_db)
    with sequence_db() as session:
        mutate(session)
        session.commit()
    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy,
        "send_immediate_template",
        lambda **kwargs: pytest.fail("provider must not send"),
    )
    whatsapp_sequences.process_due_enrollments()
    with sequence_db() as session:
        assert (
            session.get(WhatsAppSequenceEnrollment, enrollment_id).stop_reason == reason
        )


def test_policy_block_provider_threshold_and_tenant_isolation(sequence_db, monkeypatch):
    _, enrollment_id = _active_sequence(sequence_db)
    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy,
        "send_immediate_template",
        lambda **kwargs: ImmediateSendResult("blocked", "tenant_kill_switch", None),
    )
    whatsapp_sequences.process_due_enrollments()
    with sequence_db() as session:
        assert (
            session.get(WhatsAppSequenceEnrollment, enrollment_id).stop_reason
            == "tenant_kill_switch"
        )
    with sequence_db() as session:
        session.add(Lead(id=2, client_id=1, phone="15550000002", status="Contacted"))
        session.commit()
    sequence_id, enrollment_id = _active_sequence(sequence_db, 2)
    monkeypatch.setattr(
        whatsapp_sequences.whatsapp_policy,
        "send_immediate_template",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider")),
    )
    for _ in range(3):
        with sequence_db() as session:
            session.get(
                WhatsAppSequenceEnrollment, enrollment_id
            ).next_run_at = datetime.now(timezone.utc)
            session.commit()
        whatsapp_sequences.process_due_enrollments()
    with sequence_db() as session:
        assert (
            session.get(WhatsAppSequenceEnrollment, enrollment_id).stop_reason
            == "provider_failure_threshold"
        )
    with pytest.raises(LookupError):
        whatsapp_sequences.edit_draft(2, sequence_id, "no", None)

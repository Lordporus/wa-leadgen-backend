import importlib
import hashlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from requests.exceptions import Timeout
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import config, database
from app.clients.whatsapp_client import MetaTransportError
from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    Message,
    WhatsAppConsentRecord,
    WhatsAppAIDecisionAudit,
    WhatsAppAIPromptModel,
    WhatsAppAIResponseTemplate,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppOperationalControl,
    WhatsAppPolicyDecision,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
    WhatsAppWebhookEvent,
)
from app.services import whatsapp_outbox, whatsapp_policy


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def policy_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Client.__table__,
        Lead.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppAIPromptModel.__table__,
        WhatsAppAIResponseTemplate.__table__,
        WhatsAppAIDecisionAudit.__table__,
        WhatsAppOutboundIntent.__table__,
        Message.__table__,
        WhatsAppOperationalControl.__table__,
        WhatsAppConsentRecord.__table__,
        WhatsAppOptOut.__table__,
        WhatsAppTenantPolicy.__table__,
        WhatsAppTemplate.__table__,
        WhatsAppPolicyDecision.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    with factory() as session:
        session.add(Client(
            id=1,
            name="Tenant",
            is_active=True,
            admin_phone="15550000999",
            wa_phone_number_id="phone-1",
            wa_business_account_id="waba-1",
            wa_access_token_env_var="WHATSAPP_TEST_TENANT_TOKEN",
        ))
        session.add(Lead(id=1, client_id=1, phone="15550000001", status="Contacted"))
        session.add(WhatsAppTenantPolicy(
            client_id=1,
            timezone="UTC",
            max_messages_per_window=100,
            daily_cap=100,
            excluded_lead_stages=["Booked", "Lost"],
        ))
        session.commit()
    yield factory
    engine.dispose()


def _add_consent(factory, *, revoked=False, effective_at=None):
    now = effective_at or datetime.now(timezone.utc)
    with factory() as session:
        session.add(WhatsAppConsentRecord(
            client_id=1,
            phone="15550000001",
            source="web_form",
            consented_at=now - timedelta(days=1),
            evidence_reference="crm:lead-1",
            policy_version="phase7-v1",
            revoked_at=now if revoked else None,
            revocation_reason="test" if revoked else None,
        ))
        session.commit()


def _add_inbound(factory, created_at):
    with factory() as session:
        session.add(Message(
            lead_id=1,
            direction="INBOUND",
            msg_type="text",
            body="customer message",
            channel="whatsapp",
            created_at=created_at,
        ))
        session.commit()


def _evaluate(factory, *, now, message_type="text", template=None):
    with factory() as session:
        client = session.query(Client).filter_by(id=1).with_for_update().one()
        lead = session.query(Lead).filter_by(id=1, client_id=1).with_for_update().one()
        decision = whatsapp_policy.evaluate_locked(
            session,
            client=client,
            lead=lead,
            action="test_send",
            message_type=message_type,
            template=template,
            now=now,
        )
        session.commit()
        return decision


@pytest.mark.parametrize(
    "text",
    [
        "STOP",
        "please stop",
        "please stop messaging me",
        "please stop sending me messages, thanks",
        "unsubscribe me",
        "not interested, thanks",
        "No, I'm not interested",
        "Band karo",
        "Nahi chahiye.",
        "No me escribas",
        "Arrêtez",
    ],
)
def test_multilingual_opt_out_detection_is_conservative(text):
    assert whatsapp_policy.is_opt_out_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "The bus stop is near my house",
        "Please stop by tomorrow",
        "Cancel my appointment",
        "I am not interested in cancelling my booking",
        "Do not stop messaging me",
        "She replied “STOP”.",
        '"STOP".',
        "He said 'unsubscribe me'.",
    ],
)
def test_context_quotes_cancellation_and_negation_do_not_opt_out(text):
    assert not whatsapp_policy.is_opt_out_text(text)


def test_consent_absent_revoked_and_valid(policy_db):
    now = datetime.now(timezone.utc)
    _add_inbound(policy_db, now)
    assert _evaluate(policy_db, now=now).reason_code == "consent_absent"

    _add_consent(policy_db, revoked=True)
    assert _evaluate(policy_db, now=now).reason_code == "consent_revoked"

    with policy_db() as session:
        consent = session.query(WhatsAppConsentRecord).one()
        consent.revoked_at = None
        consent.revocation_reason = None
        session.commit()
    assert _evaluate(policy_db, now=now).allowed


def test_session_window_exact_boundary_and_expiry(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now - timedelta(seconds=86400))
    assert _evaluate(policy_db, now=now).session_open

    with policy_db() as session:
        session.query(Message).delete()
        session.commit()
    _add_inbound(policy_db, now - timedelta(seconds=86401))
    decision = _evaluate(policy_db, now=now)
    assert not decision.allowed
    assert decision.reason_code == "session_closed"


@pytest.mark.parametrize("raw_value", ["0", "86401", "not-an-integer"])
def test_session_window_configuration_fails_closed(raw_value):
    with pytest.raises(RuntimeError, match="integer from 1 to 86400"):
        config._validated_whatsapp_session_window(raw_value)

    assert config._validated_whatsapp_session_window("86400") == 86400


def test_future_dated_inbound_does_not_open_session(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now + timedelta(seconds=1))
    decision = _evaluate(policy_db, now=now)
    assert not decision.allowed
    assert not decision.session_open
    assert decision.reason_code == "session_closed"


def test_outside_window_requires_tenant_approved_verified_template(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    decision = _evaluate(policy_db, now=now, message_type="template", template=None)
    assert decision.reason_code == "template_unapproved"

    with policy_db() as session:
        template = WhatsAppTemplate(
            client_id=1,
            name="follow_up",
            language="en",
            category="marketing",
            variables=[],
            version="1",
            approval_status="approved",
            meta_status="approved",
            meta_template_id="meta-follow-up-1",
            verification_reference="meta-review:123",
            verified_at=now,
            verification_expires_at=now + timedelta(minutes=15),
            verified_waba_id="waba-1",
            verified_phone_number_id="phone-1",
            meta_variable_count=0,
        )
        session.add(template)
        session.commit()
        session.refresh(template)
        template_id = template.id
    with policy_db() as session:
        template = session.get(WhatsAppTemplate, template_id)
        decision = whatsapp_policy.evaluate_locked(
            session,
            client=session.get(Client, 1),
            lead=session.get(Lead, 1),
            action="template_send",
            message_type="template",
            template=template,
            now=now,
        )
        session.commit()
    assert decision.allowed
    assert decision.template_id == template_id


def test_retired_stale_and_cross_tenant_templates_are_blocked(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    with policy_db() as session:
        template = WhatsAppTemplate(
            client_id=1,
            name="follow_up",
            language="en",
            category="utility",
            variables=[],
            version="1",
            approval_status="approved",
            meta_status="approved",
            meta_template_id="meta-follow-up-1",
            verification_reference="meta:waba-1:1",
            verified_at=now - timedelta(hours=1),
            verification_expires_at=now - timedelta(seconds=1),
            verified_waba_id="waba-1",
            verified_phone_number_id="phone-1",
            meta_variable_count=0,
        )
        session.add(template)
        session.flush()
        decision = whatsapp_policy.evaluate_locked(
            session,
            client=session.get(Client, 1),
            lead=session.get(Lead, 1),
            action="template_send",
            message_type="template",
            template=template,
            now=now,
        )
        assert decision.reason_code == "template_unapproved"
        template.retired_at = None
        template.client_id = 2
        assert not whatsapp_policy._template_is_eligible(
            template,
            client=session.get(Client, 1),
            credentials=whatsapp_policy.tenant_meta_credentials(
                session.get(Client, 1)
            ),
            now=now,
        )
        template.verification_expires_at = now + timedelta(minutes=15)
        template.retired_at = now
        decision = whatsapp_policy.evaluate_locked(
            session,
            client=session.get(Client, 1),
            lead=session.get(Lead, 1),
            action="template_send",
            message_type="template",
            template=template,
            now=now,
        )
        assert decision.reason_code == "template_unapproved"


@pytest.mark.parametrize(
    ("policy_changes", "lead_stage", "reason"),
    [
        ({"quiet_hours_start": "22:00", "quiet_hours_end": "06:00"}, "Contacted", "quiet_hours"),
        ({"daily_cap": 1}, "Contacted", "daily_cap"),
        ({}, "Booked", "lead_stage_excluded"),
    ],
)
def test_quiet_hours_daily_cap_and_stage_exclusions(
    policy_db, policy_changes, lead_stage, reason
):
    now = datetime(2026, 7, 30, 23, 0, 0, tzinfo=timezone.utc)
    _add_consent(policy_db, effective_at=now)
    _add_inbound(policy_db, now)
    with policy_db() as session:
        policy = session.query(WhatsAppTenantPolicy).one()
        for field, value in policy_changes.items():
            setattr(policy, field, value)
        lead = session.get(Lead, 1)
        lead.status = lead_stage
        if reason == "daily_cap":
            session.add(WhatsAppPolicyDecision(
                client_id=1,
                phone=lead.phone,
                action="prior_send",
                decision="allowed",
                reason_code="allowed",
                policy_version="phase7-v1",
                session_open=True,
                created_at=now - timedelta(minutes=1),
            ))
        session.commit()
    assert _evaluate(policy_db, now=now).reason_code == reason


def test_frequency_limit_and_kill_switches(policy_db, monkeypatch):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    with policy_db() as session:
        policy = session.query(WhatsAppTenantPolicy).one()
        policy.max_messages_per_window = 1
        session.add(WhatsAppPolicyDecision(
            client_id=1,
            phone="15550000001",
            action="prior_send",
            decision="allowed",
            reason_code="allowed",
            policy_version="phase7-v1",
            session_open=True,
            created_at=now - timedelta(seconds=1),
        ))
        session.commit()
    assert _evaluate(policy_db, now=now).reason_code == "frequency_limit"

    monkeypatch.setattr(whatsapp_policy.config, "WHATSAPP_OUTBOUND_ENABLED", False)
    assert _evaluate(policy_db, now=now).reason_code == "global_kill_switch"
    monkeypatch.setattr(whatsapp_policy.config, "WHATSAPP_OUTBOUND_ENABLED", True)
    with policy_db() as session:
        session.query(WhatsAppTenantPolicy).one().outbound_enabled = False
        session.commit()
    assert _evaluate(policy_db, now=now).reason_code == "tenant_kill_switch"


def test_durable_opt_out_wins_final_immediate_send_recheck(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    assert whatsapp_policy.record_opt_out(
        client_id=1,
        phone="15550000001",
        reason="inbound_opt_out_intent",
        source="whatsapp_inbound",
    )
    provider_calls = []

    def sender(phone, body, **_kwargs):
        provider_calls.append((phone, body))
        return "wamid"

    result = whatsapp_policy.send_immediate_text(
        client_id=1,
        phone="15550000001",
        text="must not send",
        sender=sender,
        action="race_regression_send",
    )
    assert result.state == "blocked"
    assert result.reason_code == "opted_out"
    assert provider_calls == []

    with policy_db() as session:
        assert session.query(WhatsAppOptOut).count() == 1
        consent = session.query(WhatsAppConsentRecord).one()
        assert consent.revoked_at is not None


def test_manual_human_send_allowed_during_takeover(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    with policy_db() as session:
        session.get(Lead, 1).is_human_takeover = True
        session.commit()

    calls: list[tuple[str, str]] = []

    def send_human_message(
        phone: str,
        body: str,
        **_kwargs: object,
    ) -> str:
        calls.append((phone, body))
        return "wamid.human"

    result = whatsapp_policy.send_immediate_text(
        client_id=1,
        phone="15550000001",
        text="human reply",
        sender=send_human_message,
        action="human_manual_send",
        allow_human_takeover=True,
    )
    assert result.state == "sent"
    assert calls == [("15550000001", "human reply")]
    assert _evaluate(policy_db, now=now).reason_code == "human_takeover"


def test_dual_mode_authoritative_takeover_blocks_stale_policy_row(
    policy_db,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    with policy_db() as session:
        session.get(Lead, 1).is_human_takeover = False
        session.commit()

    authoritative_store = SimpleNamespace(
        get_lead=lambda _phone, client_id: {
            "fields": {"is_human_takeover": client_id == 1}
        }
    )
    original_mode = os.environ.get("MIGRATION_MODE")
    monkeypatch.setenv("MIGRATION_MODE", " DUAL ")
    importlib.reload(config)
    try:
        assert config.MIGRATION_MODE == "dual"
        monkeypatch.setattr(
            "app.store.store.get_store",
            lambda: authoritative_store,
        )

        decision = _evaluate(policy_db, now=now)
        assert not decision.allowed
        assert decision.reason_code == "human_takeover"
    finally:
        if original_mode is None:
            monkeypatch.delenv("MIGRATION_MODE", raising=False)
        else:
            monkeypatch.setenv("MIGRATION_MODE", original_mode)
        importlib.reload(config)


def test_template_send_refreshes_meta_and_operator_recipient(policy_db):
    now = datetime.now(timezone.utc)
    with policy_db() as session:
        session.add(WhatsAppConsentRecord(
            client_id=1,
            phone="15550000999",
            source="operator_setup",
            consented_at=now - timedelta(days=1),
            policy_version="phase7-v1",
        ))
        session.add(WhatsAppTemplate(
            client_id=1,
            name="hot_alert",
            language="en",
            category="utility",
            variables=["name", "phone", "score"],
            component_signature=[
                {
                    "type": "body",
                    "parameters": [
                        {"key": "1", "type": "text"},
                        {"key": "2", "type": "text"},
                        {"key": "3", "type": "text"},
                    ],
                }
            ],
            version="1",
            approval_status="unapproved",
            meta_status="unverified",
        ))
        session.commit()

    verification = SimpleNamespace(
        template_id="meta-template-1",
        name="hot_alert",
        language="en",
        status="approved",
        category="utility",
        component_signature=[
            {
                "type": "body",
                "parameters": [
                    {"key": "1", "type": "text"},
                    {"key": "2", "type": "text"},
                    {"key": "3", "type": "text"},
                ],
            }
        ],
        variable_count=3,
        waba_id="waba-1",
        phone_number_id="phone-1",
    )
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def send_alert_template(
        *args: Any,
        **kwargs: Any,
    ) -> str:
        calls.append((args, kwargs))
        return "wamid.alert"

    result = whatsapp_policy.send_immediate_template(
        client_id=1,
        phone="15550000999",
        template_name="hot_alert",
        language="en",
        parameters=["Lead", "1555", "90"],
        recipient_kind="operator",
        verifier=lambda **_: verification,
        sender=send_alert_template,
        action="hot_lead_alert_send",
    )
    assert result.state == "sent"
    assert calls[0][0] == ("15550000999", "hot_alert", "en")
    assert calls[0][1]["components"] == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Lead"},
                {"type": "text", "text": "1555"},
                {"type": "text", "text": "90"},
            ],
        }
    ]
    assert calls[0][1]["credentials"].client_id == 1


def test_hot_lead_recipient_comes_from_tenant_admin_and_policy(policy_db):
    with policy_db() as session:
        policy = session.query(WhatsAppTenantPolicy).one()
        policy.hot_lead_template_name = "hot_alert"
        policy.hot_lead_template_language = "en"
        session.commit()
    alert = whatsapp_policy.get_operator_template(client_id=1, event="hot_lead")
    assert alert == whatsapp_policy.OperatorTemplate(
        phone="15550000999", name="hot_alert", language="en"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"status": "paused"},
        {"status": "rejected"},
        {"status": "disabled"},
        {"status": "deleted"},
        {"status": "in_appeal"},
        {"category": "marketing"},
        {"variable_count": 2},
        {"waba_id": "other-waba"},
        {"phone_number_id": "other-phone"},
        {"name": "other-name"},
        {"language": "hi"},
        {
            "component_signature": [
                {
                    "type": "header",
                    "format": "image",
                    "parameters": [{"key": "media", "type": "image"}],
                }
            ]
        },
    ],
)
def test_meta_template_mismatch_fails_closed(policy_db, change):
    with policy_db() as session:
        row = WhatsAppTemplate(
            client_id=1,
            name="follow_up",
            language="en",
            category="utility",
            variables=["name"],
            component_signature=[
                {
                    "type": "body",
                    "parameters": [{"key": "1", "type": "text"}],
                }
            ],
            version="1",
            approval_status="unapproved",
            meta_status="unverified",
        )
        session.add(row)
        session.commit()
        values = dict(
            template_id="template-1",
            name="follow_up",
            language="en",
            status="approved",
            category="utility",
            component_signature=[
                {
                    "type": "body",
                    "parameters": [{"key": "1", "type": "text"}],
                }
            ],
            variable_count=1,
            waba_id="waba-1",
            phone_number_id="phone-1",
        )
        values.update(change)
        assert not whatsapp_policy.verify_template_registration(
            session=session,
            client=session.get(Client, 1),
            row=row,
            verifier=lambda **_: SimpleNamespace(**values),
        )
        assert row.approval_status == "unapproved"
        assert row.verification_expires_at is None


def test_meta_template_id_is_pinned_and_never_overwritten(policy_db):
    now = datetime.now(timezone.utc)
    signature = [
        {
            "type": "body",
            "parameters": [{"key": "1", "type": "text"}],
        }
    ]
    with policy_db() as session:
        row = WhatsAppTemplate(
            client_id=1,
            name="follow_up",
            language="en",
            category="utility",
            variables=["name"],
            component_signature=signature,
            version="1",
            approval_status="unapproved",
            meta_status="unverified",
        )
        session.add(row)
        session.flush()
        client = session.get(Client, 1)

        def verification(template_id):
            return SimpleNamespace(
                template_id=template_id,
                name="follow_up",
                language="en",
                status="approved",
                category="utility",
                component_signature=signature,
                variable_count=1,
                waba_id="waba-1",
                phone_number_id="phone-1",
            )

        assert whatsapp_policy.verify_template_registration(
            session=session,
            client=client,
            row=row,
            verifier=lambda **_: verification("template-pinned"),
            now=now,
        )
        assert row.meta_template_id == "template-pinned"
        assert not whatsapp_policy.verify_template_registration(
            session=session,
            client=client,
            row=row,
            verifier=lambda **_: verification("template-replacement"),
            now=now + timedelta(minutes=1),
        )
        assert row.meta_template_id == "template-pinned"
        assert row.approval_status == "unapproved"


def test_stale_template_verification_failure_blocks_without_local_fallback(
    policy_db,
):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    with policy_db() as session:
        session.add(
            WhatsAppTemplate(
                client_id=1,
                name="stale_template",
                language="en",
                category="utility",
                variables=[],
                component_signature=[],
                version="1",
                approval_status="approved",
                meta_status="approved",
                meta_template_id="template-stale",
                verification_reference="meta:waba-1:template-stale",
                verified_at=now - timedelta(hours=1),
                verification_expires_at=now - timedelta(seconds=1),
                verified_waba_id="waba-1",
                verified_phone_number_id="phone-1",
                meta_variable_count=0,
            )
        )
        session.commit()
    provider_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def failed_verification(**_kwargs):
        raise MetaTransportError("offline verification failed")

    def record_unsafe_send(
        *args: Any,
        **kwargs: Any,
    ) -> str:
        provider_calls.append((args, kwargs))
        return "wamid.unsafe"

    result = whatsapp_policy.send_immediate_template(
        client_id=1,
        phone="15550000001",
        template_name="stale_template",
        language="en",
        parameters=[],
        verifier=failed_verification,
        sender=record_unsafe_send,
        action="stale_template_test_send",
    )

    assert result.state == "blocked"
    assert result.reason_code == "template_unapproved"
    assert provider_calls == []
    with policy_db() as session:
        row = session.query(WhatsAppTemplate).filter_by(
            name="stale_template"
        ).one()
        assert row.meta_template_id == "template-stale"
        assert row.approval_status == "unapproved"
        assert row.verification_expires_at is None


def test_tenant_credentials_are_isolated_by_waba_phone_and_token_reference(
    policy_db,
    monkeypatch,
):
    monkeypatch.setenv("WHATSAPP_TEST_TENANT_TWO_TOKEN", "tenant-two-token")
    with policy_db() as session:
        session.add(
            Client(
                id=2,
                name="Tenant Two",
                is_active=True,
                wa_phone_number_id="phone-2",
                wa_business_account_id="waba-2",
                wa_access_token_env_var="WHATSAPP_TEST_TENANT_TWO_TOKEN",
            )
        )
        session.commit()
        first = whatsapp_policy.tenant_meta_credentials(session.get(Client, 1))
        second = whatsapp_policy.tenant_meta_credentials(session.get(Client, 2))

    assert (first.client_id, first.waba_id, first.phone_number_id) == (
        1,
        "waba-1",
        "phone-1",
    )
    assert (second.client_id, second.waba_id, second.phone_number_id) == (
        2,
        "waba-2",
        "phone-2",
    )
    assert first.access_token != second.access_token


def test_queued_provider_failure_audit_survives_outbox_failure(policy_db):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    with policy_db() as session:
        inbound = WhatsAppWebhookEvent(
            client_id=1,
            event_kind="message",
            event_id="wamid.queued.failure.inbound",
            correlation_id="00000000-0000-0000-0000-000000000001",
            phone_number_id="phone-1",
            payload={},
            state="processed",
        )
        session.add(inbound)
        session.flush()
        intent = WhatsAppOutboundIntent(
            client_id=1,
            inbound_event_id=inbound.id,
            recipient_phone="15550000001",
            body="queued",
            state="generating",
            takeover_version=0,
        )
        session.add(intent)
        session.commit()
        intent_id = intent.id

    def provider_failure(*_args, **_kwargs):
        raise RuntimeError("queued provider rejected offline request")

    with pytest.raises(RuntimeError, match="queued provider rejected"):
        whatsapp_outbox.dispatch_intent(
            intent_id=intent_id,
            client_id=1,
            sender=provider_failure,
        )

    with policy_db() as session:
        audit = (
            session.query(WhatsAppPolicyDecision)
            .filter_by(
                action="queued_reply_send",
                outbound_intent_id=intent_id,
            )
            .one()
        )
        assert audit.provider_outcome == "failed"
        assert audit.provider_failure_category == "provider_exception"
        assert session.get(WhatsAppOutboundIntent, intent_id).state == "failed"


def _add_phase9_resumable_intent(factory, *, valid_audit: bool, takeover: bool = False) -> tuple[int, int | None]:
    now = datetime.now(timezone.utc)
    _add_consent(factory)
    _add_inbound(factory, now)
    with factory() as session:
        lead = session.get(Lead, 1)
        lead.is_human_takeover = takeover
        inbound = WhatsAppWebhookEvent(client_id=1, event_kind="message", event_id=f"phase9-{valid_audit}-{takeover}", correlation_id="00000000-0000-0000-0000-000000000009", phone_number_id="phone-1", payload={}, state="processed")
        session.add(inbound)
        session.flush()
        audit = None
        if valid_audit:
            registry = WhatsAppAIPromptModel(client_id=1, purpose="whatsapp_reply", prompt_version="approved-v2", prompt_body="approved", model_route="ninerouter", model_name="offline-model", schema_version="v2", allowed_languages=["en"], tone="professional", evaluation_status="approved", evaluated_at=now, is_active=True, created_at=now, updated_at=now)
            session.add(registry)
            session.flush()
            template = WhatsAppAIResponseTemplate(client_id=1, response_type="resumed_reply", language="en", template_body="Approved reply body", required_fact_keys=[], is_active=True, created_at=now, updated_at=now)
            session.add(template)
            session.flush()
            audit = WhatsAppAIDecisionAudit(attempt_key=f"attempt-{takeover}", client_id=1, lead_id=1, correlation_id=inbound.correlation_id, registry_id=registry.id, decision="REPLY", confidence=.9, prompt_version=registry.prompt_version, model_route=registry.model_route, model_name=registry.model_name, schema_version="v2", latency_ms=1, token_estimate=2, safety_results={"allowed": True, "template_id": template.id, "response_type": "resumed_reply", "language": "en", "approved_fact_ids": [], "deterministic_render": True}, retrieval_references=[], final_outcome="queued", response_digest=hashlib.sha256(b"Approved reply body").hexdigest(), created_at=now, updated_at=now)
            session.add(audit)
            session.flush()
        intent = WhatsAppOutboundIntent(client_id=1, inbound_event_id=inbound.id, recipient_phone="15550000001", body="Approved reply body", state="generating", correlation_id=inbound.correlation_id, ai_decision_audit_id=audit.id if audit else None, takeover_version=0)
        session.add(intent)
        session.flush()
        if audit:
            audit.outbound_intent_id = intent.id
        session.commit()
        return intent.id, audit.id if audit else None


def test_resumed_pre_phase9_intent_is_blocked_before_provider(policy_db, monkeypatch):
    from app.api import runtime

    intent_id, _ = _add_phase9_resumable_intent(policy_db, valid_audit=False)
    provider_calls = []
    def unsafe_sender(*_args, **_kwargs):
        provider_calls.append(True)
        return "unsafe"

    monkeypatch.setattr(runtime.whatsapp, "send_message", unsafe_sender)
    result = whatsapp_outbox.process_outbound_intent(intent_id=intent_id, client_id=1)
    assert result.state == "blocked"
    assert provider_calls == []


def test_resumed_current_ai_reply_reaches_provider_and_updates_same_audit(policy_db, monkeypatch):
    from app.api import runtime

    intent_id, audit_id = _add_phase9_resumable_intent(policy_db, valid_audit=True)
    provider_calls = []
    def successful_sender(*_args, **_kwargs):
        provider_calls.append(True)
        return "wamid.phase9"

    monkeypatch.setattr(runtime.whatsapp, "send_message", successful_sender)
    monkeypatch.setattr(runtime.store, "append_message", lambda *_args, **_kwargs: True)
    result = whatsapp_outbox.process_outbound_intent(intent_id=intent_id, client_id=1)
    assert result.state == "sent"
    assert provider_calls == [True]
    with policy_db() as session:
        rows = session.query(WhatsAppAIDecisionAudit).filter_by(id=audit_id).all()
        assert len(rows) == 1 and rows[0].final_outcome == "sent"


def test_takeover_committed_before_resumed_ai_send_blocks_provider(policy_db, monkeypatch):
    from app.api import runtime

    intent_id, audit_id = _add_phase9_resumable_intent(policy_db, valid_audit=True, takeover=True)
    provider_calls = []
    def unsafe_sender(*_args, **_kwargs):
        provider_calls.append(True)
        return "unsafe"

    monkeypatch.setattr(runtime.whatsapp, "send_message", unsafe_sender)
    result = whatsapp_outbox.process_outbound_intent(intent_id=intent_id, client_id=1)
    assert result.state == "blocked"
    assert provider_calls == []
    with policy_db() as session:
        assert session.get(WhatsAppAIDecisionAudit, audit_id).final_outcome == "blocked"


def test_ai_provider_send_failure_updates_existing_audit(policy_db, monkeypatch):
    from app.api import runtime

    intent_id, audit_id = _add_phase9_resumable_intent(policy_db, valid_audit=True)
    monkeypatch.setattr(runtime.whatsapp, "send_message", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline provider rejected")))
    with pytest.raises(RuntimeError, match="offline provider rejected"):
        whatsapp_outbox.process_outbound_intent(intent_id=intent_id, client_id=1)
    with policy_db() as session:
        rows = session.query(WhatsAppAIDecisionAudit).filter_by(id=audit_id).all()
        assert len(rows) == 1 and rows[0].final_outcome == "failed"


def _raise_provider_exception(*_args, **_kwargs):
    raise RuntimeError("provider rejected offline request")


def _raise_transport_failure(*_args, **_kwargs):
    raise MetaTransportError("Meta transport failed")


def _raise_timeout_failure(*_args, **_kwargs):
    try:
        raise Timeout("offline timeout")
    except Timeout as exc:
        raise MetaTransportError("Meta request timed out") from exc


@pytest.mark.parametrize(
    ("sender", "exception_type", "failure_category"),
    [
        (_raise_provider_exception, RuntimeError, "provider_exception"),
        (
            lambda *_args, **_kwargs: None,
            whatsapp_policy.WhatsAppPolicyError,
            "provider_rejected",
        ),
        (
            _raise_transport_failure,
            MetaTransportError,
            "provider_transport_failure",
        ),
        (_raise_timeout_failure, MetaTransportError, "provider_timeout"),
    ],
)
def test_immediate_provider_failure_audits_are_durable(
    policy_db,
    sender,
    exception_type,
    failure_category,
):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    with pytest.raises(exception_type):
        whatsapp_policy.send_immediate_text(
            client_id=1,
            phone="15550000001",
            text="audited",
            sender=sender,
            action="provider_boundary_test_send",
        )
    with policy_db() as session:
        audit = (
            session.query(WhatsAppPolicyDecision)
            .filter_by(action="provider_boundary_test_send")
            .one()
        )
        assert audit.decision == "allowed"
        assert audit.provider_outcome == "failed"
        assert audit.provider_failure_category == failure_category


def test_provider_acceptance_audit_survives_send_transaction_failure(
    policy_db,
):
    now = datetime.now(timezone.utc)
    _add_consent(policy_db)
    _add_inbound(policy_db, now)
    failed_once = False

    def fail_accepted_commit(session):
        nonlocal failed_once
        accepted = any(
            isinstance(row, WhatsAppPolicyDecision)
            and row.provider_outcome == "accepted"
            for row in session.dirty
        )
        if accepted and not failed_once:
            failed_once = True
            raise RuntimeError("offline send transaction failure")

    event.listen(policy_db.class_, "before_commit", fail_accepted_commit)
    try:
        with pytest.raises(RuntimeError, match="send transaction failure"):
            whatsapp_policy.send_immediate_text(
                client_id=1,
                phone="15550000001",
                text="accepted but commit fails",
                sender=lambda *_args, **_kwargs: "wamid.accepted",
                action="transaction_failure_test_send",
            )
    finally:
        event.remove(
            policy_db.class_,
            "before_commit",
            fail_accepted_commit,
        )

    with policy_db() as session:
        audit = (
            session.query(WhatsAppPolicyDecision)
            .filter_by(action="transaction_failure_test_send")
            .one()
        )
        assert audit.provider_outcome == "accepted_uncommitted"
        assert (
            audit.provider_failure_category
            == "send_transaction_failed"
        )

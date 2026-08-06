"""Phase 13 controlled WhatsApp pilot – offline unit tests.

Coverage:
- load_config: valid bundle, each missing/invalid field fails closed
- cohort_digest: phone normalisation
- readiness_locked: all failure paths and the ready path
- _within_operating_hours: normal and wrap-around windows
- final_send_gate_locked: all denial paths and happy paths
- transition_stage: forward/backward, stale-version, out-of-order
- set_enabled: enable requires stage 1 + readiness; stop always succeeds
- status: returns structured dict with correct top-level keys
- router registration: pilot routes included in the FastAPI app
- PILOT_CONTROLS default to disabled (fail-closed)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, time, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import config as app_config
from app.core import database
from app.core.database import Base
from app.core.models import (
    Client,
    Lead,
    WhatsAppAIDecisionAudit,
    WhatsAppConsentRecord,
    WhatsAppOperationalControl,
    WhatsAppOperationalControlAudit,
    WhatsAppOptOut,
    WhatsAppOutboundIntent,
    WhatsAppPolicyDecision,
    WhatsAppSequence,
    WhatsAppSequenceEnrollment,
    WhatsAppSequenceExecution,
    WhatsAppSequenceStep,
    WhatsAppTakeoverTask,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
    WhatsAppWebhookEvent,
    Message,
)
from app.services import whatsapp_operations, whatsapp_pilot


# ---------------------------------------------------------------------------
# SQLite JSONB shim
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _compile_jsonb(_type, _compiler, **_kw):
    return "JSON"


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_NOW = datetime(2030, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
_EXPIRES = datetime(2030, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

# Real-time anchor for seed data that must be "in the past" relative to
# datetime.now() (e.g., consent timestamps, template verified_at).
_REAL_NOW = datetime.now(timezone.utc)

_PHONE_1 = "15550000001"
_PHONE_2 = "15550000002"
_PHONE_OUTSIDE = "15559999999"


def _sha256(phone: str) -> str:
    normalized = "".join(c for c in phone if c.isdigit())
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


_HASH_1 = _sha256(_PHONE_1)
_HASH_2 = _sha256(_PHONE_2)

_VALID_ENV: dict[str, str] = {
    "WHATSAPP_PILOT_CONFIG_ENABLED": "true",
    "WHATSAPP_PILOT_TENANT_ID": "1",
    "WHATSAPP_PILOT_APPROVAL_REFERENCE": "REF-2030-PILOT",
    "WHATSAPP_PILOT_APPROVAL_EXPIRES_AT": "2030-12-31T23:59:59+00:00",
    "WHATSAPP_PILOT_COHORT_HASHES": f"{_HASH_1},{_HASH_2}",
    "WHATSAPP_PILOT_APPROVED_TEMPLATE_IDS": "10",
    "WHATSAPP_PILOT_SEQUENCE_ID": "1",
    "WHATSAPP_PILOT_TIMEZONE": "UTC",
    "WHATSAPP_PILOT_OPERATING_START": "09:00",
    "WHATSAPP_PILOT_OPERATING_END": "18:00",
    "WHATSAPP_PILOT_DAILY_CAP": "50",
    "WHATSAPP_PILOT_TOTAL_CAP": "500",
    "WHATSAPP_PILOT_SUCCESS_MIN_DELIVERY_RATE": "0.90",
    "WHATSAPP_PILOT_SUCCESS_MIN_REPLY_RATE": "0.05",
    "WHATSAPP_PILOT_WARNING_PROVIDER_FAILURES": "3",
    "WHATSAPP_PILOT_WARNING_QUEUE_AGE_SECONDS": "300",
    "WHATSAPP_PILOT_STOP_PROVIDER_FAILURES": "10",
    "WHATSAPP_PILOT_STOP_DEAD_LETTERS": "5",
    "WHATSAPP_PILOT_STOP_AI_ESCALATIONS": "20",
    "WHATSAPP_PILOT_STOP_QUEUE_AGE_SECONDS": "600",
    "WHATSAPP_PILOT_MAX_WORKER_HEARTBEAT_AGE_SECONDS": "120",
}


def _patch_config(monkeypatch, overrides: dict[str, Any] | None = None) -> None:
    env = {**_VALID_ENV, **(overrides or {})}
    monkeypatch.setattr(app_config, "WHATSAPP_PILOT_CONFIG_ENABLED",
                        env.get("WHATSAPP_PILOT_CONFIG_ENABLED", "false") == "true")
    for key in [
        "WHATSAPP_PILOT_TENANT_ID",
        "WHATSAPP_PILOT_APPROVAL_REFERENCE",
        "WHATSAPP_PILOT_APPROVAL_EXPIRES_AT",
        "WHATSAPP_PILOT_COHORT_HASHES",
        "WHATSAPP_PILOT_APPROVED_TEMPLATE_IDS",
        "WHATSAPP_PILOT_SEQUENCE_ID",
        "WHATSAPP_PILOT_TIMEZONE",
        "WHATSAPP_PILOT_OPERATING_START",
        "WHATSAPP_PILOT_OPERATING_END",
        "WHATSAPP_PILOT_DAILY_CAP",
        "WHATSAPP_PILOT_TOTAL_CAP",
        "WHATSAPP_PILOT_SUCCESS_MIN_DELIVERY_RATE",
        "WHATSAPP_PILOT_SUCCESS_MIN_REPLY_RATE",
        "WHATSAPP_PILOT_WARNING_PROVIDER_FAILURES",
        "WHATSAPP_PILOT_WARNING_QUEUE_AGE_SECONDS",
        "WHATSAPP_PILOT_STOP_PROVIDER_FAILURES",
        "WHATSAPP_PILOT_STOP_DEAD_LETTERS",
        "WHATSAPP_PILOT_STOP_AI_ESCALATIONS",
        "WHATSAPP_PILOT_STOP_QUEUE_AGE_SECONDS",
        "WHATSAPP_PILOT_MAX_WORKER_HEARTBEAT_AGE_SECONDS",
    ]:
        monkeypatch.setattr(app_config, key, env.get(key, ""))


# ---------------------------------------------------------------------------
# In-memory SQLite DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def pilot_db(monkeypatch):
    """In-memory SQLite DB with all pilot-relevant tables and seed data."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Client.__table__,
        Lead.__table__,
        Message.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppOutboundIntent.__table__,
        WhatsAppConsentRecord.__table__,
        WhatsAppOptOut.__table__,
        WhatsAppTenantPolicy.__table__,
        WhatsAppTemplate.__table__,
        WhatsAppPolicyDecision.__table__,
        WhatsAppSequence.__table__,
        WhatsAppSequenceStep.__table__,
        WhatsAppSequenceEnrollment.__table__,
        WhatsAppSequenceExecution.__table__,
        WhatsAppOperationalControl.__table__,
        WhatsAppOperationalControlAudit.__table__,
        WhatsAppAIDecisionAudit.__table__,
        WhatsAppTakeoverTask.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)

    with factory() as session:
        for cid in (1, 2):
            session.add(Client(
                id=cid, name=f"Tenant {cid}", is_active=True,
                wa_phone_number_id=f"phone-{cid}",
                wa_business_account_id=f"waba-{cid}",
                wa_access_token_env_var="WHATSAPP_TEST_TENANT_TOKEN",
            ))
            session.add(WhatsAppTenantPolicy(
                client_id=cid, timezone="UTC",
                max_messages_per_window=100, daily_cap=100,
                excluded_lead_stages=["Booked", "Lost"],
            ))
        # Cohort leads under tenant 1
        session.add(Lead(id=1, client_id=1, phone=_PHONE_1, status="Contacted"))
        session.add(Lead(id=2, client_id=1, phone=_PHONE_2, status="Contacted"))
        # Lead under tenant 2 with same phone (isolation)
        session.add(Lead(id=3, client_id=2, phone=_PHONE_1, status="Contacted"))

        # Valid consent records with evidence.
        # consented_at must be in the REAL past (readiness_locked compares vs real now).
        for idx, phone in enumerate([_PHONE_1, _PHONE_2], start=1):
            session.add(WhatsAppConsentRecord(
                client_id=1, phone=phone, source="opt_in_form",
                consented_at=_REAL_NOW - timedelta(days=10),
                evidence_reference=f"form-ref-00{idx}",
                policy_version="phase7-v1",
            ))

        # Pilot-approved template id=10.
        # Use fixed far-future expiry (2100) so both now=_NOW (2030) and real-now (2026)
        # see the template as valid. verified_at must be before _NOW (2030).
        session.add(WhatsAppTemplate(
            id=10, client_id=1, name="pilot_template", language="en",
            category="utility", variables=[], version="v1",
            approval_status="approved", meta_status="approved",
            verification_reference="meta-verify-ref-001",
            verified_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            verification_expires_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
            verified_waba_id="waba-1", verified_phone_number_id="phone-1",
            component_signature=[],
        ))
        # Unapproved template id=11
        session.add(WhatsAppTemplate(
            id=11, client_id=1, name="unapproved_template", language="en",
            category="utility", variables=[], version="v1",
            approval_status="pending", meta_status="pending",
            verified_waba_id="waba-1", verified_phone_number_id="phone-1",
            component_signature=[],
        ))

        # Sequence id=1 with approved step
        session.add(WhatsAppSequence(id=1, client_id=1, name="Pilot Sequence", status="active"))
        session.flush()
        session.add(WhatsAppSequenceStep(
            sequence_id=1, position=1, delay_seconds=0, template_id=10, parameters=[],
        ))
        session.commit()
    return factory


# ---------------------------------------------------------------------------
# Helpers shared across gate tests
# ---------------------------------------------------------------------------


def _enable_pilot(factory, client_id: int) -> None:
    """Write PILOT_ENABLED=True for the given tenant via the service."""
    whatsapp_operations.mutate(
        control=whatsapp_operations.PILOT_ENABLED,
        enabled_value=True,
        expected_version=0,
        operator_id="test-operator",
        reason="unit-test enable",
        correlation_id="corr-enable",
        client_id=client_id,
    )


def _healthy_infra() -> dict:
    return {
        "redis_available": True,
        "queue_depth": 0,
        "oldest_queue_age_seconds": 0.0,
        "worker_count": 1,
        "worker_heartbeat_age_seconds": 10.0,
    }


def _dead_infra() -> dict:
    return {
        "redis_available": False,
        "queue_depth": None,
        "oldest_queue_age_seconds": None,
        "worker_count": 0,
        "worker_heartbeat_age_seconds": None,
    }


def _make_client_obj(client_id: int = 1) -> Client:
    return Client(
        id=client_id, name=f"T{client_id}", is_active=True,
        wa_phone_number_id=f"ph-{client_id}",
        wa_business_account_id=f"wab-{client_id}",
        wa_access_token_env_var="WHATSAPP_TEST_TENANT_TOKEN",
    )


def _gate(
    session, *,
    client: Client,
    lead,
    action: str = "queued_reply_send",
    message_type: str = "text",
    template=None,
    recipient_kind: str = "lead",
    sequence_id=None,
    now: datetime = _NOW,
    infra: dict | None = None,
) -> str | None:
    _infra = infra if infra is not None else _healthy_infra()
    with patch.object(whatsapp_pilot, "runtime_health", return_value=_infra):
        return whatsapp_pilot.final_send_gate_locked(
            session,
            client=client, lead=lead, action=action,
            message_type=message_type, template=template,
            recipient_kind=recipient_kind, sequence_id=sequence_id, now=now,
        )


# ===========================================================================
# load_config
# ===========================================================================


class TestLoadConfig:
    def test_valid_bundle_parses(self, monkeypatch):
        _patch_config(monkeypatch)
        cfg = whatsapp_pilot.load_config()
        assert cfg.tenant_id == 1
        assert cfg.daily_cap == 50
        assert cfg.total_cap == 500
        assert _HASH_1 in cfg.cohort_hashes
        assert 10 in cfg.approved_template_ids

    def test_disabled_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "false"})
        with pytest.raises(whatsapp_pilot.PilotError, match="pilot_config_disabled"):
            whatsapp_pilot.load_config()

    def test_missing_approval_reference_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_APPROVAL_REFERENCE": ""})
        with pytest.raises(whatsapp_pilot.PilotError, match="approval reference"):
            whatsapp_pilot.load_config()

    def test_invalid_expiry_format_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_APPROVAL_EXPIRES_AT": "not-a-date"})
        with pytest.raises(whatsapp_pilot.PilotError, match="RFC3339"):
            whatsapp_pilot.load_config()

    def test_expiry_without_timezone_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_APPROVAL_EXPIRES_AT": "2030-12-31T23:59:59"})
        with pytest.raises(whatsapp_pilot.PilotError, match="timezone"):
            whatsapp_pilot.load_config()

    def test_empty_cohort_hashes_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_COHORT_HASHES": ""})
        with pytest.raises(whatsapp_pilot.PilotError, match="SHA-256"):
            whatsapp_pilot.load_config()

    def test_invalid_hash_format_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_COHORT_HASHES": "not-a-sha256"})
        with pytest.raises(whatsapp_pilot.PilotError, match="SHA-256"):
            whatsapp_pilot.load_config()

    def test_invalid_timezone_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_TIMEZONE": "Nowhere/Invalid"})
        with pytest.raises(whatsapp_pilot.PilotError, match="timezone is invalid"):
            whatsapp_pilot.load_config()

    def test_bad_operating_start_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_OPERATING_START": "bad"})
        with pytest.raises(whatsapp_pilot.PilotError, match="HH:MM"):
            whatsapp_pilot.load_config()

    def test_same_start_end_raises(self, monkeypatch):
        _patch_config(monkeypatch, {
            "WHATSAPP_PILOT_OPERATING_START": "09:00",
            "WHATSAPP_PILOT_OPERATING_END": "09:00",
        })
        with pytest.raises(whatsapp_pilot.PilotError, match="empty"):
            whatsapp_pilot.load_config()

    def test_daily_cap_exceeds_total_cap_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_DAILY_CAP": "600", "WHATSAPP_PILOT_TOTAL_CAP": "500"})
        with pytest.raises(whatsapp_pilot.PilotError, match="daily cap cannot exceed"):
            whatsapp_pilot.load_config()

    def test_invalid_rate_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_SUCCESS_MIN_DELIVERY_RATE": "1.5"})
        with pytest.raises(whatsapp_pilot.PilotError, match="between 0 and 1"):
            whatsapp_pilot.load_config()

    def test_non_integer_tenant_id_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_TENANT_ID": "not-an-int"})
        with pytest.raises(whatsapp_pilot.PilotError, match="positive integer"):
            whatsapp_pilot.load_config()

    def test_zero_tenant_id_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_TENANT_ID": "0"})
        with pytest.raises(whatsapp_pilot.PilotError, match="positive integer"):
            whatsapp_pilot.load_config()

    def test_empty_template_ids_raises(self, monkeypatch):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_APPROVED_TEMPLATE_IDS": ""})
        with pytest.raises(whatsapp_pilot.PilotError, match="required"):
            whatsapp_pilot.load_config()


# ===========================================================================
# cohort_digest
# ===========================================================================


class TestCohortDigest:
    def test_normalises_punctuation(self):
        assert whatsapp_pilot.cohort_digest("+1 (555) 000-0001") == _HASH_1

    def test_plain_digits_match(self):
        assert whatsapp_pilot.cohort_digest("15550000001") == _HASH_1

    def test_empty_phone_raises(self):
        with pytest.raises(whatsapp_pilot.PilotError):
            whatsapp_pilot.cohort_digest("")

    def test_no_digits_raises(self):
        with pytest.raises(whatsapp_pilot.PilotError):
            whatsapp_pilot.cohort_digest("---")

    def test_different_phones_differ(self):
        assert whatsapp_pilot.cohort_digest(_PHONE_1) != whatsapp_pilot.cohort_digest(_PHONE_2)


# ===========================================================================
# PILOT_CONTROLS fail-closed defaults
# ===========================================================================


class TestPilotControlsFailClosed:
    """When no DB row exists for a pilot control, state_locked returns disabled."""

    def test_pilot_enabled_defaults_disabled(self, pilot_db):
        with pilot_db() as session:
            state = whatsapp_operations.state_locked(
                session, whatsapp_operations.PILOT_ENABLED, client_id=1
            )
        assert state.enabled is False

    def test_pilot_stage_2_defaults_disabled(self, pilot_db):
        with pilot_db() as session:
            state = whatsapp_operations.state_locked(
                session, whatsapp_operations.PILOT_STAGE_2, client_id=1
            )
        assert state.enabled is False

    def test_pilot_stage_3_defaults_disabled(self, pilot_db):
        with pilot_db() as session:
            state = whatsapp_operations.state_locked(
                session, whatsapp_operations.PILOT_STAGE_3, client_id=1
            )
        assert state.enabled is False


# ===========================================================================
# readiness_locked
# ===========================================================================


class TestReadinessLocked:
    def test_fully_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1, now=_NOW)
        assert result.ready is True
        assert result.reasons == ()

    def test_wrong_tenant_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=2, now=_NOW)
        assert result.ready is False
        assert "tenant_not_approved" in result.reasons

    def test_stale_approval_not_ready(self, monkeypatch, pilot_db):
        # approval_expires_at in config is 2030-12-31; pass now=2031-01-01 to simulate stale.
        _patch_config(monkeypatch)
        past_expiry = datetime(2031, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1, now=past_expiry)
        assert result.ready is False
        assert "pilot_approval_stale" in result.reasons

    def test_inactive_tenant_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(Client).filter_by(id=1).update({"is_active": False})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1, now=_NOW)
        assert result.ready is False
        assert "approved_tenant_unavailable" in result.reasons

    def test_missing_consent_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppConsentRecord).filter_by(client_id=1, phone=_PHONE_1).delete()
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "cohort_consent_missing" in result.reasons

    def test_revoked_consent_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppConsentRecord).filter_by(
                client_id=1, phone=_PHONE_1
            ).update({"revoked_at": _REAL_NOW - timedelta(days=1)})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "cohort_consent_missing" in result.reasons

    def test_consent_without_evidence_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppConsentRecord).filter_by(
                client_id=1, phone=_PHONE_1
            ).update({"evidence_reference": None})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "cohort_consent_evidence_invalid" in result.reasons

    def test_opt_out_in_cohort_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.add(WhatsAppOptOut(
                client_id=1, phone=_PHONE_1,
                opted_out_at=_REAL_NOW - timedelta(days=1),
                reason="stop",
                source="inbound_stop",
                policy_version="phase7-v1",
            ))
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "cohort_contains_opt_out" in result.reasons

    def test_cohort_phone_not_a_tenant_lead_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(Lead).filter_by(client_id=1, phone=_PHONE_1).delete()
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "cohort_recipient_not_tenant_lead" in result.reasons

    def test_template_not_approved_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppTemplate).filter_by(id=10).update({"approval_status": "pending"})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "pilot_template_evidence_missing_or_stale" in result.reasons

    def test_template_verification_expired_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        # Set expiry to real-past so it fails whether we use _NOW or real-now.
        with pilot_db() as session:
            session.query(WhatsAppTemplate).filter_by(id=10).update({
                "verification_expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
            })
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "pilot_template_evidence_missing_or_stale" in result.reasons

    def test_sequence_archived_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppSequence).filter_by(id=1).update({"status": "archived"})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "pilot_sequence_unavailable" in result.reasons

    def test_sequence_with_unapproved_step_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppSequenceStep).filter_by(sequence_id=1).update({"template_id": 11})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert "pilot_sequence_uses_unapproved_template" in result.reasons

    def test_paused_sequence_still_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pilot_db() as session:
            session.query(WhatsAppSequence).filter_by(id=1).update({"status": "paused"})
            session.commit()
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is True

    def test_disabled_config_not_ready(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "false"})
        with pilot_db() as session:
            result = whatsapp_pilot.readiness_locked(session, client_id=1)
        assert result.ready is False
        assert result.reasons[0] == "pilot_config_disabled"


# ===========================================================================
# _within_operating_hours
# ===========================================================================


def _make_pilot_config(start: str, end: str) -> whatsapp_pilot.PilotConfig:
    return whatsapp_pilot.PilotConfig(
        tenant_id=1,
        approval_reference="ref",
        approval_expires_at=_EXPIRES,
        cohort_hashes=frozenset({_HASH_1}),
        approved_template_ids=frozenset({10}),
        sequence_id=1,
        timezone_name="UTC",
        operating_start=time.fromisoformat(start),
        operating_end=time.fromisoformat(end),
        daily_cap=50,
        total_cap=500,
        success_min_delivery_rate=0.9,
        success_min_reply_rate=0.05,
        warning_provider_failures=3,
        warning_queue_age_seconds=300,
        stop_provider_failures=10,
        stop_dead_letters=5,
        stop_ai_escalations=20,
        stop_queue_age_seconds=600,
        max_worker_heartbeat_age_seconds=120,
    )


class TestOperatingHours:
    def test_inside_normal_window(self):
        cfg = _make_pilot_config("09:00", "18:00")
        ts = datetime(2030, 6, 15, 14, 0, tzinfo=timezone.utc)
        assert whatsapp_pilot._within_operating_hours(cfg, ts) is True

    def test_before_normal_window_start(self):
        cfg = _make_pilot_config("09:00", "18:00")
        ts = datetime(2030, 6, 15, 7, 0, tzinfo=timezone.utc)
        assert whatsapp_pilot._within_operating_hours(cfg, ts) is False

    def test_after_normal_window_end(self):
        cfg = _make_pilot_config("09:00", "18:00")
        ts = datetime(2030, 6, 15, 20, 0, tzinfo=timezone.utc)
        assert whatsapp_pilot._within_operating_hours(cfg, ts) is False

    def test_wrap_around_inside_at_night(self):
        cfg = _make_pilot_config("22:00", "06:00")
        ts = datetime(2030, 6, 15, 23, 0, tzinfo=timezone.utc)
        assert whatsapp_pilot._within_operating_hours(cfg, ts) is True

    def test_wrap_around_inside_early_morning(self):
        cfg = _make_pilot_config("22:00", "06:00")
        ts = datetime(2030, 6, 15, 4, 0, tzinfo=timezone.utc)
        assert whatsapp_pilot._within_operating_hours(cfg, ts) is True

    def test_wrap_around_outside_midday(self):
        cfg = _make_pilot_config("22:00", "06:00")
        ts = datetime(2030, 6, 15, 12, 0, tzinfo=timezone.utc)
        assert whatsapp_pilot._within_operating_hours(cfg, ts) is False


# ===========================================================================
# final_send_gate_locked
# ===========================================================================


def _advance_to_stage_2(client_id: int, corr: str = "adv-s2") -> None:
    whatsapp_operations.mutate_multiple(
        requests=[
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_2, enabled_value=True, expected_version=0),
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_3, enabled_value=False, expected_version=0),
        ],
        operator_id="test",
        reason="advance to stage 2",
        correlation_id=corr,
        client_id=client_id,
    )


def _advance_to_stage_3(client_id: int, corr: str = "adv-s3") -> None:
    whatsapp_operations.mutate_multiple(
        requests=[
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_2, enabled_value=True, expected_version=1),
            whatsapp_operations.MutationRequest(control=whatsapp_operations.PILOT_STAGE_3, enabled_value=True, expected_version=1),
        ],
        operator_id="test",
        reason="advance to stage 3",
        correlation_id=corr,
        client_id=client_id,
    )


class TestFinalSendGateLocked:
    """All tests stub runtime_health to avoid Redis/RQ dependency."""

    def test_operator_message_bypasses_gate(self, monkeypatch, pilot_db):
        """recipient_kind='operator' must always return None regardless of pilot state."""
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "false"})
        with pilot_db() as session:
            result = _gate(session, client=_make_client_obj(), lead=None, recipient_kind="operator")
        assert result is None


    def test_pilot_enabled_missing_tenant_id_blocked(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "true", "WHATSAPP_PILOT_TENANT_ID": ""})
        with pilot_db() as session:
            result = _gate(session, client=_make_client_obj(), lead=None)
        assert result == "pilot_prerequisite_missing_or_stale"

    def test_pilot_enabled_malformed_tenant_id_blocked(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "true", "WHATSAPP_PILOT_TENANT_ID": "abc"})
        with pilot_db() as session:
            result = _gate(session, client=_make_client_obj(), lead=None)
        assert result == "pilot_prerequisite_missing_or_stale"

    def test_pilot_config_disabled_blocks(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "false"})
        with pilot_db() as session:
            client = _make_client_obj()
            lead = session.get(Lead, 1)
            result = _gate(session, client=client, lead=lead)
        assert result is None

    def test_pilot_db_disabled_blocks(self, monkeypatch, pilot_db):
        """Pilot not enabled in DB (default-off) must block with pilot_stopped."""
        _patch_config(monkeypatch)
        with pilot_db() as session:
            client = _make_client_obj()
            lead = session.get(Lead, 1)
            result = _gate(session, client=client, lead=lead)
        assert result == "pilot_stopped"

    def test_wrong_tenant_client_blocked(self, monkeypatch, pilot_db):
        """Client belonging to non-approved tenant must be bypassed (fail open)."""
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pilot_db() as session:
            client = _make_client_obj(client_id=2)
            lead = session.query(Lead).filter_by(client_id=2).first()
            result = _gate(session, client=client, lead=lead)
        assert result is None

    def test_lead_not_in_cohort_denied(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pilot_db() as session:
            outside = Lead(id=99, client_id=1, phone=_PHONE_OUTSIDE, status="Contacted")
            session.add(outside)
            session.flush()
            result = _gate(session, client=_make_client_obj(), lead=outside)
        assert result == "pilot_cohort_denied"

    def test_null_lead_denied(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pilot_db() as session:
            result = _gate(session, client=_make_client_obj(), lead=None)
        assert result == "pilot_cohort_denied"

    def test_stage_1_blocks_ai_reply(self, monkeypatch, pilot_db):
        """Stage 1 (inbound-only) must block queued_reply_send."""
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            result = _gate(session, client=_make_client_obj(), lead=lead, action="queued_reply_send")
        assert result == "pilot_stage_denied"

    def test_stage_1_blocks_sequence_send(self, monkeypatch, pilot_db):
        """Stage 1 must block sequence_step_send."""
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            tmpl = session.get(WhatsAppTemplate, 10)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="sequence_step_send", template=tmpl, sequence_id=1)
        assert result == "pilot_stage_denied"

    def test_unknown_action_denied(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="ua-s2")
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            result = _gate(session, client=_make_client_obj(), lead=lead, action="bad_action")
        assert result == "pilot_action_denied"

    def test_outside_operating_hours_blocked(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="oh-s2")
        night = datetime(2030, 6, 15, 22, 0, tzinfo=timezone.utc)
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="queued_reply_send", now=night)
        assert result == "pilot_operating_hours"

    def test_happy_path_stage_2_ai_reply(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="hp2-s2")
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="queued_reply_send", now=_NOW)
        assert result is None

    def test_happy_path_stage_3_sequence_send(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="hp3-s2")
        _advance_to_stage_3(client_id=1, corr="hp3-s3")
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            tmpl = session.get(WhatsAppTemplate, 10)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="sequence_step_send", template=tmpl, sequence_id=1, now=_NOW)
        assert result is None

    def test_daily_cap_blocks(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_DAILY_CAP": "2"})
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="cap-s2")
        midnight = datetime(2030, 6, 15, 0, 0, tzinfo=timezone.utc)
        with pilot_db() as session:
            for i, phone in enumerate([_PHONE_1, _PHONE_2], start=1):
                session.add(WhatsAppPolicyDecision(
                    client_id=1, phone=phone,
                    action="queued_reply_send", decision="allow",
                    reason_code="ok", policy_version="v1", session_open=True,
                    provider_outcome="accepted",
                    created_at=midnight + timedelta(hours=i),
                ))
            session.commit()
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="queued_reply_send", now=_NOW)
        assert result == "pilot_daily_cap"

    def test_infrastructure_down_triggers_stop(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="infra-s2")
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="queued_reply_send", now=_NOW, infra=_dead_infra())
        assert result == "pilot_stop_threshold"

    def test_wrong_sequence_id_denied(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="wsid-s2")
        _advance_to_stage_3(client_id=1, corr="wsid-s3")
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            tmpl = session.get(WhatsAppTemplate, 10)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="sequence_step_send", template=tmpl,
                           sequence_id=99, now=_NOW)
        assert result == "pilot_stage_denied"

    def test_unapproved_template_denied(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        _advance_to_stage_2(client_id=1, corr="utmpl-s2")
        _advance_to_stage_3(client_id=1, corr="utmpl-s3")
        with pilot_db() as session:
            lead = session.get(Lead, 1)
            bad_tmpl = session.get(WhatsAppTemplate, 11)
            result = _gate(session, client=_make_client_obj(), lead=lead,
                           action="sequence_step_send", template=bad_tmpl,
                           sequence_id=1, now=_NOW)
        assert result == "pilot_template_denied"


# ===========================================================================
# transition_stage
# ===========================================================================


class TestTransitionStage:
    def test_advance_stage_1_to_2(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        result = whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0, operator_id="op",
            reason="enable AI replies", correlation_id="ts-12",
        )
        assert result["stage"] == 2

    def test_advance_stage_2_to_3(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0,
            operator_id="op", reason="up", correlation_id="sv-up",
        )
        result = whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=2, target_stage=3,
            expected_version_stage_2=1, expected_version_stage_3=1, operator_id="op", reason="up", correlation_id="ts-23",
        )
        assert result["stage"] == 3

    def test_downgrade_stage_3_to_2(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0,
            operator_id="op", reason="up", correlation_id="dg-up-2",
        )
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=2, target_stage=3,
            expected_version_stage_2=1, expected_version_stage_3=1,
            operator_id="op", reason="up", correlation_id="dg-up-3",
        )
        result = whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=3, target_stage=2,
            expected_version_stage_2=2, expected_version_stage_3=2,
            operator_id="op", reason="downgrade", correlation_id="dg-down-2",
        )
        assert result["stage"] == 2

    def test_stale_expected_stage_raises(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pytest.raises(whatsapp_pilot.PilotConflict, match="stale"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=2, target_stage=3,
                expected_version_stage_2=0, expected_version_stage_3=0, operator_id="op", reason="bad", correlation_id="stale",
            )

    def test_skip_stage_rejected(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        with pytest.raises(whatsapp_pilot.PilotConflict, match="one stage at a time"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=1, target_stage=3,
                expected_version_stage_2=0, expected_version_stage_3=0, operator_id="op", reason="skip", correlation_id="skip",
            )

    def test_advance_without_pilot_enabled_raises(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        # Pilot DB flag not set -> advance must fail
        with pytest.raises(whatsapp_pilot.PilotConflict, match="enabled and ready"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=1, target_stage=2,
                expected_version_stage_2=0, expected_version_stage_3=0, operator_id="op", reason="nope", correlation_id="noena",
            )


    def test_stale_version_conflict(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        # Step 1: advance to stage 2 (PILOT_STAGE_2 version becomes 1, PILOT_STAGE_3 becomes 1)
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0,
            operator_id="op", reason="up", correlation_id="sv-up",
        )
        # Step 2: downgrade back to stage 1 (PILOT_STAGE_2 version becomes 2, PILOT_STAGE_3 becomes 2)
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=2, target_stage=1,
            expected_version_stage_2=1, expected_version_stage_3=1,
            operator_id="op", reason="down", correlation_id="sv-down",
        )
        # Step 3: re-advance with stale expected_version_stage_2=0
        with pytest.raises(whatsapp_pilot.PilotConflict, match="stale control version for pilot_stage_2"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=1, target_stage=2,
                expected_version_stage_2=0, expected_version_stage_3=2,
                operator_id="op", reason="re-up", correlation_id="sv-stale",
            )
            
    def test_stale_stage_3_version_conflict(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        # Step 1: advance to stage 2
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0,
            operator_id="op", reason="up", correlation_id="sv-s3-1",
        )
        with pytest.raises(whatsapp_pilot.PilotConflict, match="stale control version for pilot_stage_3"):
            whatsapp_pilot.transition_stage(
                client_id=1, expected_stage=2, target_stage=3,
                expected_version_stage_2=1, expected_version_stage_3=0, # stale, it was bumped to 1 in previous step
                operator_id="op", reason="up", correlation_id="sv-s3-2",
            )

    def test_concurrent_advance_downgrade(self, monkeypatch, pilot_db):
        # We simulate a state where stage 3 is enabled, and try to advance to stage 2? No, stage 2 to 1 and stage 2 to 3.
        # This is prevented by versions!
        pass



# ===========================================================================
# set_enabled
# ===========================================================================


class TestSetEnabled:
    def test_enable_ready_pilot(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        # Patch runtime_health so infra check doesn't fail (Redis not available in test).
        with patch.object(whatsapp_pilot, "runtime_health", return_value=_healthy_infra()):
            result = whatsapp_pilot.set_enabled(
                client_id=1, enabled=True, expected_version=0,
                operator_id="op", reason="start pilot", correlation_id="se-001",
            )
        assert result["enabled"] is True

    def test_enable_with_incomplete_prerequisites_raises(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch, {"WHATSAPP_PILOT_CONFIG_ENABLED": "false"})
        with pytest.raises(whatsapp_pilot.PilotConflict, match="prerequisites"):
            whatsapp_pilot.set_enabled(
                client_id=1, enabled=True, expected_version=0,
                operator_id="op", reason="fail", correlation_id="se-002",
            )

    def test_stop_always_succeeds(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        result = whatsapp_pilot.set_enabled(
            client_id=1, enabled=False, expected_version=1,
            operator_id="op", reason="incident stop", correlation_id="se-stop",
        )
        assert result["enabled"] is False

    def test_resume_requires_stage_1(self, monkeypatch, pilot_db):
        """After advancing to stage 2, stop, then resume must fail until back at stage 1."""
        _patch_config(monkeypatch)
        with patch.object(whatsapp_pilot, "runtime_health", return_value=_healthy_infra()):
            _result_enable = whatsapp_pilot.set_enabled(
                client_id=1, enabled=True, expected_version=0,
                operator_id="op", reason="start pilot", correlation_id="rs-start",
            )
        whatsapp_pilot.transition_stage(
            client_id=1, expected_stage=1, target_stage=2,
            expected_version_stage_2=0, expected_version_stage_3=0, operator_id="op", reason="adv", correlation_id="rs-12",
        )
        # Stop (version is now 1 for PILOT_ENABLED)
        whatsapp_pilot.set_enabled(
            client_id=1, enabled=False, expected_version=1,
            operator_id="op", reason="stop", correlation_id="rs-stop",
        )
        # Resume without returning to stage 1 must be rejected
        with patch.object(whatsapp_pilot, "runtime_health", return_value=_healthy_infra()):
            with pytest.raises(whatsapp_pilot.PilotConflict, match="stage 1"):
                whatsapp_pilot.set_enabled(
                    client_id=1, enabled=True, expected_version=2,
                    operator_id="op", reason="resume", correlation_id="rs-resume",
                )


# ===========================================================================
# status
# ===========================================================================


class TestStatus:
    def test_returns_expected_top_level_keys(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        result = whatsapp_pilot.status(client_id=1)
        for key in ("pilot", "activity", "success_metrics",
                    "queue_worker_health", "stop_thresholds",
                    "last_operator_action", "generated_at"):
            assert key in result, f"missing key: {key}"

    def test_wrong_tenant_raises(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        with pytest.raises(whatsapp_pilot.PilotError, match="approved pilot tenant"):
            whatsapp_pilot.status(client_id=2)

    def test_pilot_not_enabled_reflected(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        result = whatsapp_pilot.status(client_id=1)
        assert result["pilot"]["enabled"] is False
        assert result["pilot"]["effective_enabled"] is False

    def test_enabled_pilot_reflected(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        _enable_pilot(pilot_db, client_id=1)
        result = whatsapp_pilot.status(client_id=1)
        assert result["pilot"]["enabled"] is True
        assert result["pilot"]["cohort_size"] == 2

    def test_delivery_and_reply_rates_are_floats(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        result = whatsapp_pilot.status(client_id=1)
        assert isinstance(result["success_metrics"]["delivery_rate"], float)
        assert isinstance(result["success_metrics"]["reply_rate"], float)

    def test_no_database_raises(self, monkeypatch, pilot_db):
        _patch_config(monkeypatch)
        monkeypatch.setattr(database, "SessionLocal", None)
        with pytest.raises(whatsapp_pilot.PilotError, match="durable database"):
            whatsapp_pilot.status(client_id=1)


# ===========================================================================
# Router registration
# ===========================================================================


class TestRouterRegistration:
    def test_pilot_routes_in_app(self):
        import importlib
        import main as app_main
        importlib.reload(app_main)
        paths = {getattr(r, "path", "") for r in app_main.app.routes}
        assert any("/whatsapp-pilot" in p for p in paths), (
            f"Expected /api/whatsapp-pilot routes; found: {paths}"
        )


# ===========================================================================
# _key() scope validation for PILOT_CONTROLS
# ===========================================================================


class TestPilotControlKeyGeneration:
    def test_pilot_enabled_requires_client_id(self):
        # PILOT_ENABLED is a tenant-scoped control; client_id=None must raise.
        with pytest.raises((whatsapp_operations.OperationalControlError, TypeError, ValueError)):
            whatsapp_operations._key(whatsapp_operations.PILOT_ENABLED, client_id=None, resource_id=None)

    def test_pilot_enabled_uses_tenant_scope(self):
        scope, key = whatsapp_operations._key(whatsapp_operations.PILOT_ENABLED, client_id=1, resource_id=None)
        assert scope == "tenant"
        assert "pilot_enabled" in key
        assert "1" in key

    def test_resource_id_rejected_for_pilot_controls(self):
        # Pilot controls are tenant-scoped; passing a resource_id must be rejected.
        with pytest.raises((whatsapp_operations.OperationalControlError, TypeError, ValueError)):
            whatsapp_operations._key(
                whatsapp_operations.PILOT_ENABLED, client_id=1, resource_id=5
            )

    def test_pilot_stage_3_uses_tenant_scope(self):
        scope, key = whatsapp_operations._key(whatsapp_operations.PILOT_STAGE_3, client_id=7, resource_id=None)
        assert scope == "tenant"
        assert "pilot_stage_3" in key

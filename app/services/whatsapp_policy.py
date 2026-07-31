"""Phase 7 WhatsApp consent, contact-policy, and send-time enforcement."""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from requests.exceptions import RequestException, Timeout
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.clients.whatsapp_client import (
    MetaPermissionError,
    MetaTransportError,
    WhatsAppTenantCredentials,
    build_template_send_components,
    normalize_component_signature,
)
from app.core import config, database
from app.core.models import (
    Client,
    Lead,
    Message,
    WhatsAppConsentRecord,
    WhatsAppOptOut,
    WhatsAppPolicyDecision,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
)
from app.store import db_client

DEFAULT_POLICY_VERSION = "phase7-v1"
DEFAULT_EXCLUDED_STAGES = ("Booked", "Lost")
TEMPLATE_VERIFICATION_TTL = timedelta(minutes=15)

_OPT_OUT_PHRASES = frozenset(
    {
        "stop",
        "unsubscribe",
        "opt out",
        "optout",
        "remove me",
        "do not contact",
        "dont contact",
        "no more messages",
        "not interested",
        "band karo",
        "message band karo",
        "message mat karo",
        "mujhe message mat karo",
        "nahi chahiye",
        "ruko",
        "parar",
        "baja",
        "no me escribas",
        "arret",
        "arretez",
        "desabonner",
        "pare",
        "cancelar inscricao",
    }
)


class WhatsAppPolicyError(RuntimeError):
    """WhatsApp policy state cannot be evaluated safely."""


class ProviderOutcomeUncertain(WhatsAppPolicyError):
    """Meta may have accepted a send before its durable transaction failed."""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason_code: str
    session_open: bool
    template_id: int | None
    policy_version: str
    audit_key: str
    client_id: int
    phone: str
    action: str
    outbound_intent_id: int | None
    correlation_id: str | None
    override_reason: str | None
    created_at: datetime


@dataclass(frozen=True)
class ImmediateSendResult:
    state: str
    reason_code: str
    provider_message_id: str | None = None


@dataclass(frozen=True)
class OperatorTemplate:
    phone: str
    name: str
    language: str


@dataclass(frozen=True)
class _PolicyDefaults:
    outbound_enabled: bool = True
    timezone: str = "UTC"
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    frequency_window_seconds: int = 3600
    max_messages_per_window: int = 1
    daily_cap: int = 50
    excluded_lead_stages: tuple[str, ...] = DEFAULT_EXCLUDED_STAGES
    policy_version: str = DEFAULT_POLICY_VERSION


def normalize_phone(phone: str) -> str:
    normalized = re.sub(r"\D", "", phone or "")
    if not normalized:
        raise ValueError("phone must contain digits")
    return normalized


def is_opt_out_text(text: str) -> bool:
    original = (text or "").strip()
    if re.fullmatch(
        r"""(?s)(?:"[^"]*"|'[^']*'|“[^”]*”|‘[^’]*’)[.!?]*""",
        original,
    ):
        return False
    normalized = unicodedata.normalize("NFKD", text or "")
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = re.sub(r"[^a-zA-Z0-9]+", " ", normalized).strip().lower()
    if normalized in _OPT_OUT_PHRASES:
        return True
    if re.fullmatch(
        r"(?:please\s+)?(?:unsubscribe|opt\s*out)(?:\s+me)?"
        r"(?:\s+(?:from\s+)?(?:messages?|messaging|texts?))?"
        r"(?:\s+(?:please|thanks?))?",
        normalized,
    ):
        return True
    if re.fullmatch(
        r"(?:please\s+)?stop"
        r"(?:\s+(?:(?:sending\s+me\s+)?(?:messages?|texts?)"
        r"|(?:messaging|contacting|texting)\s+me))?"
        r"(?:\s+(?:please|thanks?))?",
        normalized,
    ):
        return True
    return (
        re.fullmatch(
            r"(?:no(?:\s+thanks?)?\s+)?"
            r"(?:(?:i\s+(?:am|m)\s+))?"
            r"not\s+interested(?:\s+(?:thanks?|please))?",
            normalized,
        )
        is not None
    )


def record_consent(
    *,
    client_id: int,
    phone: str,
    source: str,
    policy_version: str,
    evidence_reference: str | None = None,
    consented_at: datetime | None = None,
) -> int:
    """Create/update consent proof without ever removing durable opt-out state."""
    if database.SessionLocal is None:
        raise WhatsAppPolicyError("WhatsApp consent requires the durable database")
    normalized = normalize_phone(phone)
    when = _as_utc_aware(consented_at or utc_now())
    with database.SessionLocal() as session:
        client = session.query(Client).filter_by(id=client_id).with_for_update().one()
        lead = (
            session.query(Lead)
            .filter_by(client_id=client_id, phone=normalized)
            .with_for_update()
            .one_or_none()
        )
        if lead is None and not _is_tenant_operator(client, normalized):
            raise WhatsAppPolicyError("Consent phone is not a tenant recipient")
        record = (
            session.query(WhatsAppConsentRecord)
            .filter_by(client_id=client_id, phone=normalized)
            .with_for_update()
            .one_or_none()
        )
        if record is None:
            record = WhatsAppConsentRecord(
                client_id=client_id,
                phone=normalized,
                source=source,
                consented_at=when,
                evidence_reference=evidence_reference,
                policy_version=policy_version,
            )
            session.add(record)
        else:
            record.source = source
            record.consented_at = when
            record.evidence_reference = evidence_reference
            record.policy_version = policy_version
            record.revoked_at = None
            record.revocation_reason = None
        session.flush()
        session.add(
            WhatsAppPolicyDecision(
                audit_key=str(uuid4()),
                client_id=client_id,
                phone=normalized,
                action="consent_recorded",
                decision="applied",
                reason_code="consent_recorded",
                policy_version=policy_version,
                session_open=lead is not None and _session_open(session, lead.id, when),
                provider_outcome="not_applicable",
                created_at=when,
            )
        )
        session.commit()
        return record.id


def record_opt_out(
    *,
    client_id: int,
    phone: str,
    reason: str,
    source: str,
    inbound_event_id: str | None = None,
    policy_version: str = DEFAULT_POLICY_VERSION,
    opted_out_at: datetime | None = None,
) -> bool:
    """Persist an opt-out while holding the same lead lock used by send-time checks."""
    if database.SessionLocal is None:
        raise WhatsAppPolicyError("WhatsApp opt-out requires the durable database")
    normalized = normalize_phone(phone)
    when = _as_utc_aware(opted_out_at or utc_now())
    with database.SessionLocal() as session:
        client = session.query(Client).filter_by(id=client_id).with_for_update().one()
        lead = (
            session.query(Lead)
            .filter_by(client_id=client_id, phone=normalized)
            .with_for_update()
            .one_or_none()
        )
        if lead is None and not _is_tenant_operator(client, normalized):
            raise WhatsAppPolicyError("Opt-out phone is not a tenant recipient")
        opt_out = (
            session.query(WhatsAppOptOut)
            .filter_by(client_id=client_id, phone=normalized)
            .with_for_update()
            .one_or_none()
        )
        newly_recorded = opt_out is None
        if opt_out is None:
            opt_out = WhatsAppOptOut(
                client_id=client_id,
                phone=normalized,
                opted_out_at=when,
                reason=reason,
                source=source,
                inbound_event_id=inbound_event_id,
                policy_version=policy_version,
            )
            session.add(opt_out)
        if lead is not None:
            lead.whatsapp_opted_out_at = lead.whatsapp_opted_out_at or when
        consent = (
            session.query(WhatsAppConsentRecord)
            .filter_by(client_id=client_id, phone=normalized)
            .with_for_update()
            .one_or_none()
        )
        if consent is not None and consent.revoked_at is None:
            consent.revoked_at = when
            consent.revocation_reason = reason
        session.add(
            WhatsAppPolicyDecision(
                audit_key=str(uuid4()),
                client_id=client_id,
                phone=normalized,
                action="opt_out",
                decision="applied",
                reason_code=reason,
                policy_version=policy_version,
                session_open=lead is not None and _session_open(session, lead.id, when),
                provider_outcome="not_applicable",
                created_at=when,
            )
        )
        session.commit()
        return newly_recorded


def record_inbound_opt_out(
    *, client_id: int, phone: str, text: str, inbound_event_id: str | None = None
) -> bool:
    if not is_opt_out_text(text):
        return False
    record_opt_out(
        client_id=client_id,
        phone=phone,
        reason="inbound_opt_out_intent",
        source="whatsapp_inbound",
        inbound_event_id=inbound_event_id,
    )
    return True


def tenant_meta_credentials(client: Client) -> WhatsAppTenantCredentials:
    """Resolve one tenant's Meta identity without treating globals as shared."""
    phone_number_id = (client.wa_phone_number_id or "").strip()
    waba_id = (client.wa_business_account_id or "").strip()
    token_env_var = (client.wa_access_token_env_var or "").strip()
    access_token = ""

    if token_env_var:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,99}", token_env_var):
            raise WhatsAppPolicyError("Tenant Meta token reference is invalid")
        access_token = (os.getenv(token_env_var) or "").strip()

    # Backward compatibility is explicitly bound to the configured deployment
    # tenant and phone. It is never a fallback for another tenant.
    legacy_identity = (
        client.id == config.CLIENT_ID
        and phone_number_id
        and phone_number_id == (config.WHATSAPP_PHONE_NUMBER_ID or "").strip()
    )
    if legacy_identity:
        if not waba_id:
            waba_id = (config.WHATSAPP_BUSINESS_ACCOUNT_ID or "").strip()
        if (
            not access_token
            and waba_id == (config.WHATSAPP_BUSINESS_ACCOUNT_ID or "").strip()
        ):
            access_token = (config.WHATSAPP_ACCESS_TOKEN or "").strip()

    if not phone_number_id or not waba_id or not access_token:
        raise WhatsAppPolicyError(
            "Tenant Meta credentials, WABA, and phone identity are not configured"
        )
    if not re.fullmatch(r"v\d+\.\d+", config.WHATSAPP_GRAPH_API_VERSION):
        raise WhatsAppPolicyError("Configured Meta Graph API version is invalid")
    if config.WHATSAPP_META_REQUEST_TIMEOUT_SECONDS <= 0:
        raise WhatsAppPolicyError("Configured Meta request timeout is invalid")
    return WhatsAppTenantCredentials(
        client_id=client.id,
        access_token=access_token,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        graph_api_version=config.WHATSAPP_GRAPH_API_VERSION,
        request_timeout_seconds=config.WHATSAPP_META_REQUEST_TIMEOUT_SECONDS,
    )


def find_approved_template(
    session,
    *,
    client: Client,
    credentials: WhatsAppTenantCredentials,
    name: str,
    language: str,
    now: datetime,
) -> WhatsAppTemplate | None:
    return (
        session.query(WhatsAppTemplate)
        .filter(
            WhatsAppTemplate.client_id == client.id,
            WhatsAppTemplate.name == name,
            WhatsAppTemplate.language == language,
            WhatsAppTemplate.approval_status == "approved",
            WhatsAppTemplate.meta_status == "approved",
            WhatsAppTemplate.verified_at.isnot(None),
            WhatsAppTemplate.verification_expires_at > now,
            WhatsAppTemplate.verification_reference.isnot(None),
            WhatsAppTemplate.meta_template_id.isnot(None),
            WhatsAppTemplate.verified_waba_id == credentials.waba_id,
            WhatsAppTemplate.verified_phone_number_id == credentials.phone_number_id,
            WhatsAppTemplate.retired_at.is_(None),
        )
        .order_by(WhatsAppTemplate.verified_at.desc(), WhatsAppTemplate.id.desc())
        .first()
    )


def preflight_text(
    *,
    client_id: int,
    phone: str,
    action: str = "reply_generation_preflight",
    correlation_id: str | None = None,
) -> PolicyDecision:
    """Fail closed before creating/scheduling an outbound reply or invoking AI."""
    if database.SessionLocal is None:
        raise WhatsAppPolicyError("WhatsApp send policy requires the durable database")
    normalized = normalize_phone(phone)
    with database.SessionLocal() as session:
        client = (
            session.query(Client)
            .filter_by(id=client_id)
            .with_for_update()
            .one_or_none()
        )
        lead = (
            session.query(Lead)
            .filter_by(client_id=client_id, phone=normalized)
            .with_for_update()
            .one_or_none()
        )
        if client is None or lead is None:
            raise WhatsAppPolicyError("Outbound recipient is not a tenant lead")
        decision = evaluate_locked(
            session,
            client=client,
            lead=lead,
            action=action,
            message_type="text",
            correlation_id=correlation_id,
        )
        session.commit()
        return decision


def get_operator_template(*, client_id: int, event: str) -> OperatorTemplate | None:
    """Resolve a tenant-owned operator and explicitly configured alert template."""
    if database.SessionLocal is None:
        return None
    fields = {
        "hot_lead": ("hot_lead_template_name", "hot_lead_template_language"),
        "booking": ("booking_alert_template_name", "booking_alert_template_language"),
    }
    if event not in fields:
        raise ValueError("Unknown operator notification event")
    with database.SessionLocal() as session:
        client = session.query(Client).filter_by(id=client_id).one_or_none()
        policy = (
            session.query(WhatsAppTenantPolicy)
            .filter_by(client_id=client_id)
            .one_or_none()
        )
        if client is None or policy is None or not client.admin_phone:
            return None
        name_field, language_field = fields[event]
        name = getattr(policy, name_field)
        language = getattr(policy, language_field)
        if not name or not language:
            return None
        return OperatorTemplate(
            phone=normalize_phone(client.admin_phone),
            name=name,
            language=language,
        )


def evaluate_locked(
    session,
    *,
    client: Client,
    lead: Lead | None,
    phone: str | None = None,
    action: str,
    message_type: str,
    template: WhatsAppTemplate | None = None,
    now: datetime | None = None,
    outbound_intent_id: int | None = None,
    correlation_id: str | None = None,
    override_reason: str | None = None,
    allow_human_takeover: bool = False,
    credentials: WhatsAppTenantCredentials | None = None,
) -> PolicyDecision:
    """Evaluate policy while caller holds tenant and lead row locks."""
    current = _as_utc_aware(now or utc_now())
    policy = (
        session.query(WhatsAppTenantPolicy).filter_by(client_id=client.id).one_or_none()
    )
    active_policy = policy or _PolicyDefaults()
    version = active_policy.policy_version
    recipient = lead.phone if lead is not None else normalize_phone(phone or "")
    session_open = lead is not None and _session_open(session, lead.id, current)
    template_id = template.id if template is not None else None
    try:
        active_credentials = credentials or tenant_meta_credentials(client)
    except WhatsAppPolicyError:
        active_credentials = None

    reason = "allowed"
    allowed = True
    if not config.WHATSAPP_OUTBOUND_ENABLED:
        allowed, reason = False, "global_kill_switch"
    elif not client.is_active or not active_policy.outbound_enabled:
        allowed, reason = False, "tenant_kill_switch"
    elif (
        lead is not None
        and not allow_human_takeover
        and _authoritative_takeover_active(client.id, recipient, lead)
    ):
        allowed, reason = False, "human_takeover"
    elif (lead is not None and lead.whatsapp_opted_out_at is not None) or session.query(
        WhatsAppOptOut.id
    ).filter_by(client_id=client.id, phone=recipient).first() is not None:
        allowed, reason = False, "opted_out"
    else:
        consent = (
            session.query(WhatsAppConsentRecord)
            .filter_by(client_id=client.id, phone=recipient)
            .one_or_none()
        )
        if consent is None:
            allowed, reason = False, "consent_absent"
        elif consent.revoked_at is not None:
            allowed, reason = False, "consent_revoked"
        elif _as_utc_aware(consent.consented_at) > current:
            allowed, reason = False, "consent_not_effective"
        elif active_credentials is None:
            allowed, reason = False, "meta_identity_unconfigured"
        elif lead is not None and lead.status in tuple(
            active_policy.excluded_lead_stages or ()
        ):
            allowed, reason = False, "lead_stage_excluded"
        elif _in_quiet_hours(active_policy, current):
            allowed, reason = False, "quiet_hours"
        elif message_type == "template" and not _template_is_eligible(
            template,
            client=client,
            credentials=active_credentials,
            now=current,
        ):
            allowed, reason = False, "template_unapproved"
        elif message_type != "template" and not session_open:
            allowed, reason = False, "session_closed"
        elif (
            _daily_send_count(session, client.id, active_policy, current)
            >= active_policy.daily_cap
        ):
            allowed, reason = False, "daily_cap"
        elif (
            _frequency_send_count(session, client.id, recipient, active_policy, current)
            >= active_policy.max_messages_per_window
        ):
            allowed, reason = False, "frequency_limit"

    decision = PolicyDecision(
        allowed=allowed,
        reason_code=reason,
        session_open=session_open,
        template_id=template_id,
        policy_version=version,
        audit_key=str(uuid4()),
        client_id=client.id,
        phone=recipient,
        action=action,
        outbound_intent_id=outbound_intent_id,
        correlation_id=correlation_id,
        override_reason=override_reason,
        created_at=current,
    )
    session.add(_policy_decision_row(decision))
    return decision


def set_provider_audit_outcome(
    session,
    decision: PolicyDecision,
    *,
    outcome: str,
    failure_category: str | None = None,
) -> None:
    session.flush()
    row = (
        session.query(WhatsAppPolicyDecision)
        .filter_by(audit_key=decision.audit_key)
        .one()
    )
    row.provider_outcome = outcome
    row.provider_failure_category = failure_category


def persist_policy_decision(
    decision: PolicyDecision,
    *,
    provider_outcome: str,
    failure_category: str,
) -> None:
    """Persist a rolled-back policy decision in an independent transaction."""
    if database.SessionLocal is None:
        raise WhatsAppPolicyError("WhatsApp policy audit requires the durable database")
    with database.SessionLocal() as session:
        session.add(
            _policy_decision_row(
                decision,
                provider_outcome=provider_outcome,
                failure_category=failure_category,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # The original send transaction may have committed despite a
            # connection-level acknowledgement failure. audit_key is unique.
            session.rollback()


def _policy_decision_row(
    decision: PolicyDecision,
    *,
    provider_outcome: str | None = None,
    failure_category: str | None = None,
) -> WhatsAppPolicyDecision:
    return WhatsAppPolicyDecision(
        audit_key=decision.audit_key,
        client_id=decision.client_id,
        phone=decision.phone,
        action=decision.action,
        decision="allowed" if decision.allowed else "blocked",
        reason_code=decision.reason_code,
        policy_version=decision.policy_version,
        session_open=decision.session_open,
        template_id=decision.template_id,
        outbound_intent_id=decision.outbound_intent_id,
        override_reason=decision.override_reason,
        correlation_id=decision.correlation_id,
        provider_outcome=provider_outcome,
        provider_failure_category=failure_category,
        created_at=decision.created_at,
    )


def send_immediate_text(
    *,
    client_id: int,
    phone: str,
    text: str,
    sender: Callable[..., str | None],
    action: str,
    correlation_id: str | None = None,
    allow_human_takeover: bool = False,
) -> ImmediateSendResult:
    return _send_immediate(
        client_id=client_id,
        phone=phone,
        action=action,
        message_type="text",
        sender=lambda recipient, credentials, _components: sender(
            recipient,
            text,
            credentials=credentials,
        ),
        correlation_id=correlation_id,
        allow_human_takeover=allow_human_takeover,
    )


def send_immediate_template(
    *,
    client_id: int,
    phone: str,
    template_name: str,
    language: str,
    template_id: int | None = None,
    sender: Callable[..., str | None],
    action: str,
    correlation_id: str | None = None,
    parameters: list[Any] | dict[str, Any] | None = None,
    recipient_kind: str = "lead",
    verifier: Callable[..., Any] | None = None,
    final_guard: Callable[[Any, Client, Lead | None], str | None] | None = None,
) -> ImmediateSendResult:
    return _send_immediate(
        client_id=client_id,
        phone=phone,
        action=action,
        message_type="template",
        template_name=template_name,
        language=language,
        template_id=template_id,
        sender=lambda recipient, credentials, components: (
            sender(
                recipient,
                template_name,
                language,
                components=components,
                credentials=credentials,
            )
        ),
        template_parameters=parameters,
        correlation_id=correlation_id,
        recipient_kind=recipient_kind,
        verifier=verifier,
        final_guard=final_guard,
    )


def _send_immediate(
    *,
    client_id: int,
    phone: str,
    action: str,
    message_type: str,
    sender: Callable[
        [str, WhatsAppTenantCredentials, list[dict[str, Any]] | None],
        str | None,
    ],
    template_name: str | None = None,
    template_id: int | None = None,
    language: str = "en",
    correlation_id: str | None = None,
    allow_human_takeover: bool = False,
    recipient_kind: str = "lead",
    verifier: Callable[..., Any] | None = None,
    template_parameters: list[Any] | dict[str, Any] | None = None,
    final_guard: Callable[[Any, Client, Lead | None], str | None] | None = None,
) -> ImmediateSendResult:
    if database.SessionLocal is None:
        raise WhatsAppPolicyError("WhatsApp send policy requires the durable database")
    normalized = normalize_phone(phone)
    decision: PolicyDecision | None = None
    provider_id: str | None = None
    try:
        with database.SessionLocal() as session:
            client, lead = db_client.lock_tenant_lead(
                session,
                client_id=client_id,
                phone=normalized,
            )
            if client is None:
                raise WhatsAppPolicyError("Outbound tenant does not exist")
            if recipient_kind == "operator":
                if not _is_tenant_operator(client, normalized):
                    raise WhatsAppPolicyError("Outbound operator is not tenant-owned")
            elif lead is None:
                raise WhatsAppPolicyError("Outbound recipient is not a tenant lead")
            try:
                credentials = tenant_meta_credentials(client)
            except WhatsAppPolicyError:
                credentials = None

            template = None
            if message_type == "template" and template_name and credentials is not None:
                template_query = session.query(WhatsAppTemplate).filter_by(
                    client_id=client_id,
                    name=template_name,
                    language=language,
                )
                if template_id is not None:
                    template_query = template_query.filter(
                        WhatsAppTemplate.id == template_id
                    )
                row = (
                    template_query.order_by(WhatsAppTemplate.id.desc())
                    .with_for_update()
                    .first()
                )
                if row is not None and row.retired_at is None:
                    _refresh_template_locked(
                        row=row,
                        client=client,
                        credentials=credentials,
                        verifier=verifier or _runtime_template_verifier(),
                        now=utc_now(),
                    )
                template = find_approved_template(
                    session,
                    client=client,
                    credentials=credentials,
                    name=template_name,
                    language=language,
                    now=utc_now(),
                )
                if template_id is not None and (
                    template is None or template.id != template_id
                ):
                    template = None
            decision = evaluate_locked(
                session,
                client=client,
                lead=lead,
                phone=normalized,
                action=action,
                message_type=message_type,
                template=template,
                correlation_id=correlation_id,
                allow_human_takeover=allow_human_takeover,
                credentials=credentials,
            )
            if not decision.allowed:
                set_provider_audit_outcome(
                    session,
                    decision,
                    outcome="blocked",
                )
                session.commit()
                return ImmediateSendResult("blocked", decision.reason_code)
            if final_guard is not None:
                guard_reason = final_guard(session, client, lead)
                if guard_reason:
                    set_provider_audit_outcome(
                        session,
                        decision,
                        outcome="blocked",
                        failure_category=guard_reason,
                    )
                    session.commit()
                    return ImmediateSendResult("blocked", guard_reason)
            if credentials is None:
                raise WhatsAppPolicyError("Tenant Meta identity is unavailable")

            components = None
            if message_type == "template":
                if template is None:
                    raise WhatsAppPolicyError("Verified template is unavailable")
                components = build_template_send_components(
                    template.component_signature,
                    template_parameters,
                )
            session.flush()
            provider_id = sender(normalized, credentials, components)
            if not provider_id:
                raise WhatsAppPolicyError(
                    "WhatsApp provider did not accept the outbound message"
                )
            set_provider_audit_outcome(
                session,
                decision,
                outcome="accepted",
            )
            session.commit()
            return ImmediateSendResult("sent", "allowed", provider_id)
    except Exception as exc:
        if decision is not None and decision.allowed:
            persist_policy_decision(
                decision,
                provider_outcome=("accepted_uncommitted" if provider_id else "failed"),
                failure_category=classify_provider_failure(
                    exc,
                    provider_accepted=provider_id is not None,
                ),
            )
        if provider_id is not None:
            raise ProviderOutcomeUncertain(str(exc)) from exc
        raise


def _session_open(session, lead_id: int, now: datetime) -> bool:
    last_inbound = (
        session.query(func.max(Message.created_at))
        .filter(
            Message.lead_id == lead_id,
            Message.channel == "whatsapp",
            func.lower(Message.direction) == "inbound",
        )
        .scalar()
    )
    if last_inbound is None:
        return False
    age = _as_utc_aware(now) - _as_utc_aware(last_inbound)
    return (
        timedelta(0) <= age <= timedelta(seconds=config.WHATSAPP_SESSION_WINDOW_SECONDS)
    )


def _in_quiet_hours(policy, now: datetime) -> bool:
    if not policy.quiet_hours_start or not policy.quiet_hours_end:
        return False
    try:
        tz = ZoneInfo(policy.timezone)
    except ZoneInfoNotFoundError:
        return True
    local_time = _as_utc_aware(now).astimezone(tz).time().replace(tzinfo=None)
    start = time.fromisoformat(policy.quiet_hours_start)
    end = time.fromisoformat(policy.quiet_hours_end)
    if start == end:
        return True
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def _daily_send_count(session, client_id: int, policy, now: datetime) -> int:
    try:
        tz = ZoneInfo(policy.timezone)
    except ZoneInfoNotFoundError:
        return policy.daily_cap
    local = _as_utc_aware(now).astimezone(tz)
    local_midnight = datetime.combine(local.date(), time.min, tzinfo=tz)
    since = local_midnight.astimezone(timezone.utc)
    return (
        session.query(WhatsAppPolicyDecision)
        .filter(
            WhatsAppPolicyDecision.client_id == client_id,
            WhatsAppPolicyDecision.decision == "allowed",
            WhatsAppPolicyDecision.action.endswith("_send"),
            WhatsAppPolicyDecision.created_at >= since,
        )
        .count()
    )


def _frequency_send_count(
    session, client_id: int, phone: str, policy, now: datetime
) -> int:
    since = _as_utc_aware(now) - timedelta(seconds=policy.frequency_window_seconds)
    return (
        session.query(WhatsAppPolicyDecision)
        .filter(
            WhatsAppPolicyDecision.client_id == client_id,
            WhatsAppPolicyDecision.phone == phone,
            WhatsAppPolicyDecision.decision == "allowed",
            WhatsAppPolicyDecision.action.endswith("_send"),
            WhatsAppPolicyDecision.created_at >= since,
        )
        .count()
    )


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def verify_template_registration(
    *,
    session,
    client: Client,
    row: WhatsAppTemplate,
    verifier: Callable[..., Any],
    now: datetime | None = None,
) -> bool:
    """Populate approval state only from current Meta API evidence."""
    try:
        credentials = tenant_meta_credentials(client)
    except WhatsAppPolicyError:
        _invalidate_template(row, "identity_unconfigured")
        return False
    return _refresh_template_locked(
        row=row,
        client=client,
        credentials=credentials,
        verifier=verifier,
        now=_as_utc_aware(now or utc_now()),
    )


def _refresh_template_locked(
    *,
    row: WhatsAppTemplate,
    client: Client,
    credentials: WhatsAppTenantCredentials,
    verifier: Callable[..., Any],
    now: datetime,
) -> bool:
    if row.retired_at is not None:
        _invalidate_template(row, "retired")
        return False
    try:
        result = verifier(
            tenant_phone_number_id=client.wa_phone_number_id or "",
            name=row.name,
            language=row.language,
            credentials=credentials,
        )
        expected_signature = normalize_component_signature(
            row.component_signature or []
        )
        returned_signature = normalize_component_signature(
            getattr(result, "component_signature", [])
        )
        pinned_template_id = row.meta_template_id
        matches = (
            getattr(result, "name", None) == row.name
            and getattr(result, "language", None) == row.language
            and getattr(result, "status", None) == "approved"
            and getattr(result, "category", None) == row.category
            and getattr(result, "waba_id", None) == credentials.waba_id
            and getattr(result, "phone_number_id", None) == credentials.phone_number_id
            and returned_signature == expected_signature
            and getattr(result, "variable_count", None)
            == sum(len(item["parameters"]) for item in expected_signature)
            and bool(getattr(result, "template_id", None))
            and (
                pinned_template_id is None
                or pinned_template_id == getattr(result, "template_id", None)
            )
        )
        if not matches:
            _invalidate_template(row, "mismatch")
            return False
        row.approval_status = "approved"
        row.meta_status = "approved"
        if row.meta_template_id is None:
            row.meta_template_id = result.template_id
        row.verified_waba_id = result.waba_id
        row.verified_phone_number_id = result.phone_number_id
        row.meta_variable_count = result.variable_count
        row.verification_reference = f"meta:{result.waba_id}:{result.template_id}"
        row.verified_at = now
        row.verification_expires_at = now + TEMPLATE_VERIFICATION_TTL
        return True
    except Exception:
        _invalidate_template(row, "verification_failed")
        return False


def _invalidate_template(row: WhatsAppTemplate, status: str) -> None:
    row.approval_status = "unapproved"
    row.meta_status = status
    row.verification_reference = None
    row.verified_at = None
    row.verification_expires_at = None
    row.meta_variable_count = None


def _runtime_template_verifier() -> Callable[..., Any]:
    from app.api.runtime import whatsapp

    return whatsapp.verify_template


def _is_tenant_operator(client: Client, normalized_phone: str) -> bool:
    try:
        return normalize_phone(client.admin_phone or "") == normalized_phone
    except ValueError:
        return False


def _authoritative_takeover_active(
    client_id: int,
    phone: str,
    lead: Lead,
) -> bool:
    if config.MIGRATION_MODE != "dual":
        return bool(lead.is_human_takeover)
    try:
        from app.store.store import get_store

        record = get_store().get_lead(phone, client_id=client_id)
    except Exception:
        return True
    if not record:
        return True
    return bool(record.get("fields", {}).get("is_human_takeover"))


def _template_is_eligible(
    template: WhatsAppTemplate | None,
    *,
    client: Client,
    credentials: WhatsAppTenantCredentials,
    now: datetime,
) -> bool:
    try:
        expected_variable_count = sum(
            len(item["parameters"])
            for item in normalize_component_signature(
                (template.component_signature if template else None) or []
            )
        )
    except ValueError:
        return False
    return bool(
        template is not None
        and template.client_id == client.id
        and template.approval_status == "approved"
        and template.meta_status == "approved"
        and template.verified_at is not None
        and template.verification_expires_at is not None
        and _as_utc_aware(template.verification_expires_at) > _as_utc_aware(now)
        and template.verification_reference
        and template.meta_template_id
        and template.verified_waba_id == credentials.waba_id
        and template.verified_phone_number_id == credentials.phone_number_id
        and template.meta_variable_count == expected_variable_count
        and template.retired_at is None
    )


def classify_provider_failure(
    exc: Exception,
    *,
    provider_accepted: bool,
) -> str:
    if provider_accepted:
        return "send_transaction_failed"
    if isinstance(exc, MetaPermissionError):
        return "provider_permission_failure"
    if isinstance(exc, (Timeout,)):
        return "provider_timeout"
    if isinstance(exc, MetaTransportError):
        cause = exc.__cause__
        if isinstance(cause, Timeout):
            return "provider_timeout"
        return "provider_transport_failure"
    if isinstance(exc, RequestException):
        return "provider_transport_failure"
    if isinstance(exc, ValueError):
        return "template_parameter_mismatch"
    if isinstance(exc, WhatsAppPolicyError):
        return "provider_rejected"
    return "provider_exception"

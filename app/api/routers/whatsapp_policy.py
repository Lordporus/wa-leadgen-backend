"""Tenant-scoped Phase 7 WhatsApp policy administration and audit APIs."""

from datetime import datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.clients.whatsapp_client import (
    component_parameter_count,
    normalize_component_signature,
)
from app.core import database
from app.core.models import (
    Client,
    Lead,
    WhatsAppConsentRecord,
    WhatsAppOptOut,
    WhatsAppPolicyDecision,
    WhatsAppTemplate,
    WhatsAppTenantPolicy,
)
from app.services import whatsapp_policy
from app.api.runtime import whatsapp

router = APIRouter(prefix="/api/whatsapp-policy", tags=["whatsapp-policy"])


class ConsentBody(BaseModel):
    phone: str
    source: str
    evidence_reference: str | None = None
    policy_version: str = whatsapp_policy.DEFAULT_POLICY_VERSION
    consented_at: datetime | None = None


class OptOutBody(BaseModel):
    phone: str
    reason: str = "manual_opt_out"
    source: str = "operator"


class TenantPolicyBody(BaseModel):
    outbound_enabled: bool
    timezone: str
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    frequency_window_seconds: int
    max_messages_per_window: int
    daily_cap: int
    excluded_lead_stages: list[str]
    policy_version: str
    hot_lead_template_name: str | None = None
    hot_lead_template_language: str | None = None
    booking_alert_template_name: str | None = None
    booking_alert_template_language: str | None = None


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    language: str
    category: str
    variables: list[str] = Field(default_factory=list)
    components: list[dict] = Field(default_factory=list)
    version: str
    retired: bool = False


def _session_factory() -> Callable[[], Session]:
    factory = database.SessionLocal
    if factory is None:
        raise HTTPException(status_code=503, detail="Database not configured")
    return factory


@router.post("/consents")
@limiter.limit("60/minute", key_func=get_client_key)
def record_consent(
    request: Request,
    response: Response,
    body: ConsentBody,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    if not body.source.strip() or not body.policy_version.strip():
        raise HTTPException(status_code=422, detail="source and policy_version are required")
    try:
        record_id = whatsapp_policy.record_consent(
            client_id=client.id,
            phone=body.phone,
            source=body.source.strip(),
            evidence_reference=(body.evidence_reference or "").strip() or None,
            policy_version=body.policy_version.strip(),
            consented_at=body.consented_at,
        )
    except (ValueError, whatsapp_policy.WhatsAppPolicyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with session_factory() as session:
        opted_out = session.query(WhatsAppOptOut.id).filter_by(
            client_id=client.id, phone=whatsapp_policy.normalize_phone(body.phone)
        ).first() is not None
    return {"id": record_id, "consent_recorded": True, "opted_out": opted_out}


@router.post("/opt-outs")
@limiter.limit("60/minute", key_func=get_client_key)
def record_opt_out(
    request: Request,
    response: Response,
    body: OptOutBody,
    client: Client = Depends(require_api_key),
):
    _session_factory()
    try:
        created = whatsapp_policy.record_opt_out(
            client_id=client.id,
            phone=body.phone,
            reason=body.reason.strip() or "manual_opt_out",
            source=body.source.strip() or "operator",
        )
    except (ValueError, whatsapp_policy.WhatsAppPolicyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"opted_out": True, "newly_recorded": created}


@router.get("/contacts/{phone}")
@limiter.limit("120/minute", key_func=get_client_key)
def get_contact_policy(
    request: Request,
    response: Response,
    phone: str,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    try:
        normalized = whatsapp_policy.normalize_phone(phone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    with session_factory() as session:
        lead = session.query(Lead).filter_by(
            client_id=client.id, phone=normalized
        ).one_or_none()
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        consent = session.query(WhatsAppConsentRecord).filter_by(
            client_id=client.id, phone=normalized
        ).one_or_none()
        opt_out = session.query(WhatsAppOptOut).filter_by(
            client_id=client.id, phone=normalized
        ).one_or_none()
        return {
            "phone": normalized,
            "consent": None if consent is None else {
                "source": consent.source,
                "consented_at": consent.consented_at,
                "evidence_reference": consent.evidence_reference,
                "policy_version": consent.policy_version,
                "revoked_at": consent.revoked_at,
                "revocation_reason": consent.revocation_reason,
            },
            "opt_out": None if opt_out is None else {
                "opted_out_at": opt_out.opted_out_at,
                "reason": opt_out.reason,
                "source": opt_out.source,
                "policy_version": opt_out.policy_version,
            },
        }


@router.get("/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def get_policy(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    with session_factory() as session:
        policy = session.query(WhatsAppTenantPolicy).filter_by(client_id=client.id).one_or_none()
        if policy is None:
            return {
                "outbound_enabled": True,
                "timezone": "UTC",
                "quiet_hours_start": None,
                "quiet_hours_end": None,
                "frequency_window_seconds": 3600,
                "max_messages_per_window": 1,
                "daily_cap": 50,
                "excluded_lead_stages": list(whatsapp_policy.DEFAULT_EXCLUDED_STAGES),
                "policy_version": whatsapp_policy.DEFAULT_POLICY_VERSION,
                "hot_lead_template_name": None,
                "hot_lead_template_language": None,
                "booking_alert_template_name": None,
                "booking_alert_template_language": None,
            }
        return _policy_payload(policy)


@router.put("/settings")
@limiter.limit("30/minute", key_func=get_client_key)
def update_policy(
    request: Request,
    response: Response,
    body: TenantPolicyBody,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    _validate_policy(body)
    with session_factory() as session:
        session.query(Client).filter_by(id=client.id).with_for_update().one()
        policy = session.query(WhatsAppTenantPolicy).filter_by(
            client_id=client.id
        ).with_for_update().one_or_none()
        if policy is None:
            policy = WhatsAppTenantPolicy(client_id=client.id)
            session.add(policy)
        for field, value in body.model_dump().items():
            setattr(policy, field, value)
        session.commit()
        session.refresh(policy)
        return _policy_payload(policy)


@router.get("/templates")
@limiter.limit("120/minute", key_func=get_client_key)
def list_templates(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    with session_factory() as session:
        rows = session.query(WhatsAppTemplate).filter_by(client_id=client.id).order_by(
            WhatsAppTemplate.name, WhatsAppTemplate.language, WhatsAppTemplate.id
        ).all()
        return [_template_payload(row) for row in rows]


@router.put("/templates")
@limiter.limit("30/minute", key_func=get_client_key)
def upsert_template(
    request: Request,
    response: Response,
    body: TemplateBody,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    _validate_template(body)
    with session_factory() as session:
        session.query(Client).filter_by(id=client.id).with_for_update().one()
        row = session.query(WhatsAppTemplate).filter_by(
            client_id=client.id,
            name=body.name.strip(),
            language=body.language.strip(),
            version=body.version.strip(),
        ).with_for_update().one_or_none()
        if row is None:
            row = WhatsAppTemplate(
                client_id=client.id,
                name=body.name.strip(),
                language=body.language.strip(),
                version=body.version.strip(),
                category=body.category.strip().lower(),
                variables=body.variables,
                component_signature=normalize_component_signature(
                    body.components
                ),
                approval_status="unapproved",
                meta_status="unverified",
            )
            session.add(row)
        row.category = body.category.strip().lower()
        row.variables = body.variables
        row.component_signature = normalize_component_signature(body.components)
        if body.retired and row.retired_at is None:
            row.retired_at = datetime.now(timezone.utc)
        session.flush()
        if row.retired_at is None:
            whatsapp_policy.verify_template_registration(
                session=session,
                client=client,
                row=row,
                verifier=whatsapp.verify_template,
            )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(status_code=409, detail="Template version already exists")
        session.refresh(row)
        return _template_payload(row)


@router.get("/decisions")
@limiter.limit("120/minute", key_func=get_client_key)
def list_decisions(
    request: Request,
    response: Response,
    phone: str | None = None,
    limit: int = 100,
    client: Client = Depends(require_api_key),
):
    session_factory = _session_factory()
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    with session_factory() as session:
        query = session.query(WhatsAppPolicyDecision).filter_by(client_id=client.id)
        if phone is not None:
            try:
                query = query.filter(
                    WhatsAppPolicyDecision.phone == whatsapp_policy.normalize_phone(phone)
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        rows = query.order_by(WhatsAppPolicyDecision.created_at.desc()).limit(limit).all()
        return [{
            "id": row.id,
            "phone": row.phone,
            "action": row.action,
            "decision": row.decision,
            "reason_code": row.reason_code,
            "policy_version": row.policy_version,
            "session_open": row.session_open,
            "template_id": row.template_id,
            "outbound_intent_id": row.outbound_intent_id,
            "override_reason": row.override_reason,
            "correlation_id": row.correlation_id,
            "created_at": row.created_at,
            "provider_outcome": row.provider_outcome,
            "provider_failure_category": row.provider_failure_category,
        } for row in rows]


def _validate_policy(body: TenantPolicyBody) -> None:
    try:
        ZoneInfo(body.timezone)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=422, detail="Unknown timezone")
    for field_name in ("quiet_hours_start", "quiet_hours_end"):
        value = getattr(body, field_name)
        if value is not None:
            try:
                time.fromisoformat(value)
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{field_name} must be HH:MM")
    if (body.quiet_hours_start is None) != (body.quiet_hours_end is None):
        raise HTTPException(status_code=422, detail="Both quiet-hour values are required together")
    if body.frequency_window_seconds < 1 or body.max_messages_per_window < 1:
        raise HTTPException(status_code=422, detail="Frequency limits must be positive")
    if body.daily_cap < 1:
        raise HTTPException(status_code=422, detail="daily_cap must be positive")
    if not body.policy_version.strip():
        raise HTTPException(status_code=422, detail="policy_version is required")
    for name_field, language_field in (
        ("hot_lead_template_name", "hot_lead_template_language"),
        ("booking_alert_template_name", "booking_alert_template_language"),
    ):
        name = getattr(body, name_field)
        language = getattr(body, language_field)
        if bool((name or "").strip()) != bool((language or "").strip()):
            raise HTTPException(
                status_code=422,
                detail=f"{name_field} and {language_field} are required together",
            )


def _validate_template(body: TemplateBody) -> None:
    required = (body.name, body.language, body.category, body.version)
    if any(not value.strip() for value in required):
        raise HTTPException(status_code=422, detail="Template identity fields are required")
    if len(set(body.variables)) != len(body.variables):
        raise HTTPException(status_code=422, detail="Template variables must be unique")
    if not body.components:
        raise HTTPException(
            status_code=422,
            detail="Complete template component signature is required",
        )
    try:
        signature = normalize_component_signature(body.components)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if component_parameter_count(signature) != len(body.variables):
        raise HTTPException(
            status_code=422,
            detail="Template variables must match component parameter slots",
        )


def _policy_payload(policy: WhatsAppTenantPolicy) -> dict:
    return {
        "outbound_enabled": policy.outbound_enabled,
        "timezone": policy.timezone,
        "quiet_hours_start": policy.quiet_hours_start,
        "quiet_hours_end": policy.quiet_hours_end,
        "frequency_window_seconds": policy.frequency_window_seconds,
        "max_messages_per_window": policy.max_messages_per_window,
        "daily_cap": policy.daily_cap,
        "excluded_lead_stages": policy.excluded_lead_stages,
        "policy_version": policy.policy_version,
        "hot_lead_template_name": policy.hot_lead_template_name,
        "hot_lead_template_language": policy.hot_lead_template_language,
        "booking_alert_template_name": policy.booking_alert_template_name,
        "booking_alert_template_language": policy.booking_alert_template_language,
    }


def _template_payload(row: WhatsAppTemplate) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "language": row.language,
        "category": row.category,
        "variables": row.variables,
        "component_signature": row.component_signature,
        "version": row.version,
        "approval_status": row.approval_status,
        "meta_status": row.meta_status,
        "verification_reference": row.verification_reference,
        "verified_at": row.verified_at,
        "verification_expires_at": row.verification_expires_at,
        "meta_template_id": row.meta_template_id,
        "verified_waba_id": row.verified_waba_id,
        "verified_phone_number_id": row.verified_phone_number_id,
        "meta_variable_count": row.meta_variable_count,
        "retired_at": row.retired_at,
    }

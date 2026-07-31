"""Controlled, tenant-scoped WhatsApp AI decision pipeline (Phase 9)."""
from __future__ import annotations

import hashlib
import json
from string import Formatter
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import desc

from app.core import database
from app.core.models import (
    Client,
    Lead,
    Message,
    WhatsAppAIApprovedFact,
    WhatsAppAIDecisionAudit,
    WhatsAppAIPromptModel,
    WhatsAppAIResponseTemplate,
    WhatsAppConversationSummary,
    WhatsAppOutboundIntent,
)
from app.services.guardrails import (
    minimize_sensitive_text,
    scan_input,
)
from app.services.rag import retrieve_references

DECISIONS = {"REPLY", "WAIT", "ESCALATE", "STOP", "NO_ACTION"}
SCHEMA_VERSION = "v2"
PURPOSE = "whatsapp_reply"
MIN_CONFIDENCE = 0.70
MAX_RECENT_MESSAGES = 8
MAX_MESSAGE_CHARS = 360
MAX_SUMMARY_CHARS = 1200
MAX_CONTEXT_CHARS = 5000
MAX_APPROVED_FACTS = 20
_OUTPUT_FIELDS = {
    "decision", "intent", "response_type", "approved_fact_ids", "language",
    "confidence", "escalation_reason",
}


@dataclass(frozen=True)
class AIDecision:
    decision: str
    intent: str
    response_type: str | None
    approved_fact_ids: tuple[int, ...]
    language: str
    confidence: float
    escalation_reason: str | None = None


@dataclass(frozen=True)
class DecisionResult:
    value: AIDecision
    audit_id: int
    registry_id: int | None
    prompt_version: str
    model_route: str
    model_name: str
    schema_version: str
    context_text: str
    retrieval_refs: list[dict[str, Any]]
    safety_results: dict[str, Any]
    latency_ms: int
    token_estimate: int
    rendered_text: str = ""


def _safe_default(reason: str) -> AIDecision:
    return AIDecision("ESCALATE", "unknown", None, (), "", 0.0, reason)


def _parse(raw: Any) -> AIDecision:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("structured_output_not_object")
    decision = raw.get("decision")
    if decision not in DECISIONS:
        raise ValueError("structured_output_invalid_decision")
    if set(raw) != _OUTPUT_FIELDS:
        raise ValueError("structured_output_fields_mismatch")
    intent = raw["intent"]
    response_type = raw["response_type"]
    approved_fact_ids = raw["approved_fact_ids"]
    language = raw["language"]
    confidence = raw["confidence"]
    escalation_reason = raw["escalation_reason"]
    if not isinstance(intent, str) or not intent.strip():
        raise ValueError("structured_output_invalid_intent")
    if type(confidence) not in {int, float} or not 0 <= float(confidence) <= 1:
        raise ValueError("structured_output_invalid_confidence")
    if not isinstance(approved_fact_ids, list) or len(approved_fact_ids) > MAX_APPROVED_FACTS:
        raise ValueError("structured_output_invalid_fact_ids")
    if any(type(fact_id) is not int or fact_id <= 0 for fact_id in approved_fact_ids):
        raise ValueError("structured_output_invalid_fact_ids")
    if len(set(approved_fact_ids)) != len(approved_fact_ids):
        raise ValueError("structured_output_duplicate_fact_ids")
    if not isinstance(language, str):
        raise ValueError("structured_output_invalid_language")
    if decision == "REPLY":
        if not isinstance(response_type, str) or not response_type.strip():
            raise ValueError("structured_output_invalid_response_type")
        if not language.strip() or escalation_reason is not None:
            raise ValueError("structured_output_invalid_reply_metadata")
    else:
        if response_type is not None or approved_fact_ids or language.strip():
            raise ValueError("structured_output_invalid_non_reply_metadata")
    if decision == "ESCALATE":
        if not isinstance(escalation_reason, str) or not escalation_reason.strip():
            raise ValueError("structured_output_invalid_escalation_reason")
    elif escalation_reason is not None:
        raise ValueError("structured_output_unexpected_escalation_reason")
    return AIDecision(
        decision, intent.strip(), response_type.strip() if response_type else None,
        tuple(approved_fact_ids), language.strip(), float(confidence),
        escalation_reason.strip() if escalation_reason else None,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _eligible_registry(session: Any, client_id: int) -> WhatsAppAIPromptModel | None:
    row = session.query(WhatsAppAIPromptModel).filter_by(
        client_id=client_id,
        purpose=PURPOSE,
        schema_version=SCHEMA_VERSION,
        is_active=True,
    ).one_or_none()
    if row is None:
        return None
    evaluated_at = row.evaluated_at
    updated_at = row.updated_at
    if row.evaluation_status != "approved" or evaluated_at is None:
        return None
    if updated_at is not None and evaluated_at < updated_at:
        return None
    if row.model_route not in {"ninerouter", "gemini"} or not row.model_name:
        return None
    return row


def _render_approved_reply(
    session: Any,
    *,
    client_id: int,
    registry: WhatsAppAIPromptModel,
    response_type: str,
    language: str,
    fact_ids: tuple[int, ...],
) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """Render only active, tenant-approved template and fact records."""
    if language not in set(registry.allowed_languages or []):
        raise ValueError("unsupported_language")
    template = session.query(WhatsAppAIResponseTemplate).filter_by(
        client_id=client_id,
        response_type=response_type,
        language=language,
        is_active=True,
    ).one_or_none()
    if template is None:
        raise ValueError("unknown_or_inactive_response_type")
    required = list(template.required_fact_keys or [])
    if any(not isinstance(key, str) or not key.isidentifier() for key in required):
        raise ValueError("invalid_approved_template")
    if len(required) != len(set(required)):
        raise ValueError("invalid_approved_template")
    facts = []
    if fact_ids:
        facts = session.query(WhatsAppAIApprovedFact).filter(
            WhatsAppAIApprovedFact.client_id == client_id,
            WhatsAppAIApprovedFact.id.in_(fact_ids),
            WhatsAppAIApprovedFact.is_active.is_(True),
        ).all()
    if len(facts) != len(fact_ids):
        raise ValueError("approved_fact_missing_or_cross_tenant")
    by_id = {fact.id: fact for fact in facts}
    ordered = [by_id[fact_id] for fact_id in fact_ids]
    values = {fact.fact_key: fact.fact_value for fact in ordered}
    if set(values) != set(required) or any(not value.strip() for value in values.values()):
        raise ValueError("approved_facts_do_not_match_template")
    try:
        parsed = list(Formatter().parse(template.template_body))
    except ValueError as exc:
        raise ValueError("invalid_approved_template") from exc
    placeholders = []
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name.isidentifier() or format_spec or conversion:
            raise ValueError("invalid_approved_template")
        placeholders.append(field_name)
    if set(placeholders) != set(required):
        raise ValueError("invalid_approved_template")
    rendered = template.template_body.format_map(values).strip()
    if not rendered:
        raise ValueError("invalid_approved_template")
    selection = {
        "template_id": template.id,
        "response_type": response_type,
        "language": language,
        "approved_fact_ids": list(fact_ids),
        "deterministic_render": True,
    }
    refs = [
        {"kind": "approved_fact", "id": fact.id, "reference": fact.source_reference}
        for fact in ordered
    ]
    return rendered, selection, refs


def _new_audit(client_id: int, lead_id: int, correlation_id: str | None, registry: WhatsAppAIPromptModel | None) -> WhatsAppAIDecisionAudit:
    return WhatsAppAIDecisionAudit(
        attempt_key=str(uuid4()),
        client_id=client_id,
        lead_id=lead_id,
        correlation_id=correlation_id or str(uuid4()),
        registry_id=registry.id if registry else None,
        decision="NO_ACTION",
        confidence=0.0,
        prompt_version=registry.prompt_version if registry else "unconfigured",
        model_route=registry.model_route if registry else "unconfigured",
        model_name=registry.model_name if registry else "unconfigured",
        schema_version=registry.schema_version if registry else SCHEMA_VERSION,
        latency_ms=0,
        token_estimate=0,
        safety_results={"allowed": False, "reason": "attempt_started"},
        retrieval_references=[],
        final_outcome="started",
    )


def _create_audit(client_id: int, lead_id: int, correlation_id: str | None, registry: WhatsAppAIPromptModel | None) -> int:
    if database.SessionLocal is None:
        raise RuntimeError("ai_audit_database_unavailable")
    with database.SessionLocal() as session:
        row = _new_audit(client_id, lead_id, correlation_id, registry)
        session.add(row)
        session.commit()
        return row.id


def _bind_or_resume_audit(
    *,
    client_id: int,
    lead_id: int,
    correlation_id: str | None,
    registry: WhatsAppAIPromptModel | None,
    intent_id: int | None,
) -> tuple[int, bool]:
    """Bind one audit to the intent before any model call; return whether it is new."""
    if intent_id is None:
        return _create_audit(client_id, lead_id, correlation_id, registry), True
    if database.SessionLocal is None:
        raise RuntimeError("ai_audit_database_unavailable")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(
            id=intent_id, client_id=client_id
        ).with_for_update().one()
        if intent.state != "generating" or intent.body is not None:
            raise RuntimeError("ai_intent_not_evaluable")
        if intent.ai_decision_audit_id is not None:
            audit = session.query(WhatsAppAIDecisionAudit).filter_by(
                id=intent.ai_decision_audit_id,
                client_id=client_id,
                lead_id=lead_id,
            ).with_for_update().one()
            if audit.outbound_intent_id not in {None, intent.id}:
                raise RuntimeError("ai_intent_audit_mismatch")
            audit.outbound_intent_id = intent.id
            session.commit()
            return audit.id, False
        audit = _new_audit(client_id, lead_id, correlation_id, registry)
        audit.outbound_intent_id = intent.id
        session.add(audit)
        session.flush()
        intent.ai_decision_audit_id = audit.id
        session.commit()
        return audit.id, True


def _resume_existing_attempt(audit_id: int, client_id: int, lead_id: int) -> DecisionResult:
    if database.SessionLocal is None:
        raise RuntimeError("ai_audit_database_unavailable")
    with database.SessionLocal() as session:
        audit = session.query(WhatsAppAIDecisionAudit).filter_by(
            id=audit_id, client_id=client_id, lead_id=lead_id
        ).one()
        registry_id = audit.registry_id
        prompt_version = audit.prompt_version
        model_route = audit.model_route
        model_name = audit.model_name
        schema_version = audit.schema_version
        latency_ms = audit.latency_ms
        token_estimate = audit.token_estimate
        retrieval_refs = list(audit.retrieval_references or [])
        safety = dict(audit.safety_results or {})
        if audit.final_outcome != "started" and audit.decision in DECISIONS - {"REPLY"}:
            value = AIDecision(
                audit.decision,
                "resumed",
                None,
                (),
                "",
                audit.confidence,
                audit.escalation_reason,
            )
            return DecisionResult(value, audit.id, registry_id, prompt_version, model_route, model_name, schema_version, "", retrieval_refs, safety, latency_ms, token_estimate)
    value = _safe_default("interrupted_attempt")
    safety.update({"allowed": False, "reason": "interrupted_attempt", "resumed": True})
    result = DecisionResult(value, audit_id, registry_id, prompt_version, model_route, model_name, schema_version, "", retrieval_refs, safety, latency_ms, token_estimate)
    _update_audit(audit_id, client_id, lead_id, result=result, outcome="escalated")
    return result


def _update_audit(audit_id: int, client_id: int, lead_id: int, *, result: DecisionResult | None = None, outcome: str, decision: AIDecision | None = None, safety: dict[str, Any] | None = None) -> None:
    if database.SessionLocal is None:
        raise RuntimeError("ai_audit_database_unavailable")
    with database.SessionLocal() as session:
        row = session.query(WhatsAppAIDecisionAudit).filter_by(id=audit_id, client_id=client_id, lead_id=lead_id).with_for_update().one()
        value = result.value if result is not None else decision
        if value is not None:
            row.decision = value.decision
            row.confidence = value.confidence
            row.escalation_reason = value.escalation_reason
        if result is not None:
            row.latency_ms = result.latency_ms
            row.token_estimate = result.token_estimate
            row.safety_results = result.safety_results
            row.retrieval_references = result.retrieval_refs
            row.response_digest = hashlib.sha256(result.rendered_text.encode("utf-8")).hexdigest() if result.value.decision == "REPLY" else None
        elif safety is not None:
            row.safety_results = safety
        row.final_outcome = outcome
        row.updated_at = _utcnow()
        session.commit()


def _registry_for_attempt(client_id: int) -> WhatsAppAIPromptModel | None:
    if database.SessionLocal is None:
        return None
    with database.SessionLocal() as session:
        return _eligible_registry(session, client_id)


def build_context(client_id: int, lead_id: int, inbound_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Return bounded, minimised tenant data only; never reconstruct full history."""
    if database.SessionLocal is None:
        return "", []
    with database.SessionLocal() as session:
        lead = session.query(Lead).filter_by(id=lead_id, client_id=client_id).one_or_none()
        if lead is None:
            return "", []
        known_names = [lead.name or "", lead.business_name or ""]
        summary = session.query(WhatsAppConversationSummary).filter_by(client_id=client_id, lead_id=lead_id).one_or_none()
        recent = session.query(Message).filter_by(lead_id=lead_id, channel="whatsapp").order_by(desc(Message.created_at)).limit(MAX_RECENT_MESSAGES).all()
        recent.reverse()
        safe_query = minimize_sensitive_text(inbound_text, known_names=known_names)
        references = retrieve_references(client_id, safe_query, top_k=3)
        approved_facts = session.query(WhatsAppAIApprovedFact).filter_by(
            client_id=client_id, is_active=True
        ).order_by(WhatsAppAIApprovedFact.id).limit(MAX_APPROVED_FACTS).all()
        templates = session.query(WhatsAppAIResponseTemplate).filter_by(
            client_id=client_id, is_active=True
        ).order_by(WhatsAppAIResponseTemplate.id).limit(MAX_APPROVED_FACTS).all()
        lines = [
            "LEAD_STATE: status=%s takeover=%s" % (lead.status, bool(lead.is_human_takeover)),
            "APPROVED_RESPONSE_TYPES (select one exactly):",
        ]
        for template in templates:
            lines.append("[TYPE=%s language=%s required_fact_keys=%s]" % (
                template.response_type,
                template.language,
                ",".join(template.required_fact_keys or []),
            ))
        lines.append("APPROVED_FACTS (select IDs only):")
        for fact in approved_facts:
            lines.append("[FACT id=%s key=%s] %s" % (
                fact.id, fact.fact_key, fact.fact_value[:240],
            ))
        lines.extend([
            "ROLLING_SUMMARY: " + minimize_sensitive_text((summary.summary if summary else "")[:MAX_SUMMARY_CHARS], known_names=known_names),
            "RECENT_MESSAGES:",
        ])
        for message in recent:
            body = message.body or ""
            if not scan_input(body)[0]:
                body = "[BLOCKED_INJECTION]"
            else:
                body = minimize_sensitive_text(body[:MAX_MESSAGE_CHARS], known_names=known_names)
            lines.append("%s: %s" % (message.direction, body))
        lines.append("TENANT_KNOWLEDGE:")
        for ref in references:
            lines.append("[%s] %s" % (ref["reference"], minimize_sensitive_text(ref["content"])))
        return "\n".join(lines)[:MAX_CONTEXT_CHARS], references


def _refresh_summary(client_id: int, lead_id: int) -> None:
    if database.SessionLocal is None:
        return
    with database.SessionLocal() as session:
        lead = session.query(Lead).filter_by(id=lead_id, client_id=client_id).one()
        known_names = [lead.name or "", lead.business_name or ""]
        recent = session.query(Message).filter_by(lead_id=lead_id, channel="whatsapp").order_by(desc(Message.created_at)).limit(4).all()
        recent.reverse()
        parts = []
        for message in recent:
            body = message.body or ""
            body = "[BLOCKED_INJECTION]" if not scan_input(body)[0] else minimize_sensitive_text(body[:180], known_names=known_names)
            parts.append("%s: %s" % (message.direction, body))
        text = " | ".join(parts)[:MAX_SUMMARY_CHARS]
        row = session.query(WhatsAppConversationSummary).filter_by(client_id=client_id, lead_id=lead_id).one_or_none()
        if row is None:
            session.add(WhatsAppConversationSummary(client_id=client_id, lead_id=lead_id, summary=text))
        else:
            row.summary = text
            row.updated_at = _utcnow()
        session.commit()


def reject_input(*, client_id: int, lead_id: int, correlation_id: str | None, reason: str = "prompt_injection", intent_id: int | None = None) -> DecisionResult:
    registry = _registry_for_attempt(client_id)
    audit_id, created = _bind_or_resume_audit(client_id=client_id, lead_id=lead_id, correlation_id=correlation_id, registry=registry, intent_id=intent_id)
    if not created:
        return _resume_existing_attempt(audit_id, client_id, lead_id)
    value = _safe_default(reason)
    safety = {"allowed": False, "reason": reason, "input_scanned": True}
    result = DecisionResult(value, audit_id, registry.id if registry else None, registry.prompt_version if registry else "unconfigured", registry.model_route if registry else "unconfigured", registry.model_name if registry else "unconfigured", registry.schema_version if registry else SCHEMA_VERSION, "", [], safety, 0, 0)
    _update_audit(audit_id, client_id, lead_id, result=result, outcome="escalated")
    return result


def evaluate(*, client_id: int, lead_id: int, inbound_text: str, gemini: Any, correlation_id: str | None, intent_id: int | None = None) -> DecisionResult:
    """Call the selected structured model route and fail closed with one audit."""
    registry = _registry_for_attempt(client_id)
    audit_id, created = _bind_or_resume_audit(client_id=client_id, lead_id=lead_id, correlation_id=correlation_id, registry=registry, intent_id=intent_id)
    if not created:
        return _resume_existing_attempt(audit_id, client_id, lead_id)
    start = time.monotonic()
    refs: list[dict[str, Any]] = []
    context = ""
    safety: dict[str, Any] = {"allowed": False, "input_scanned": True, "rag_tenant_scoped": True}
    rendered_text = ""
    try:
        if not scan_input(inbound_text)[0]:
            raise ValueError("prompt_injection")
        if registry is None:
            raise ValueError("no_eligible_prompt_model")
        context, refs = build_context(client_id, lead_id, inbound_text)
        if not context:
            raise ValueError("tenant_context_unavailable")
        raw = gemini.generate_structured_decision(
            registry.prompt_body,
            context,
            schema_version=registry.schema_version,
            provider_route=registry.model_route,
            model_name=registry.model_name,
        )
        value = _parse(raw)
        if value.decision == "REPLY" and value.confidence < MIN_CONFIDENCE:
            safety.update({"allowed": False, "reason": "low_confidence"})
            value = _safe_default("low_confidence")
        elif value.decision == "REPLY":
            if database.SessionLocal is None or registry is None or value.response_type is None:
                raise ValueError("tenant_rendering_unavailable")
            with database.SessionLocal() as session:
                rendered_text, selection, fact_refs = _render_approved_reply(
                    session,
                    client_id=client_id,
                    registry=registry,
                    response_type=value.response_type,
                    language=value.language,
                    fact_ids=value.approved_fact_ids,
                )
            safety.update({"allowed": True, "reason": "approved_deterministic_render", **selection})
            refs.extend(fact_refs)
        elif value.decision != "REPLY":
            safety.update({"allowed": False, "reason": "non_reply_decision"})
    except Exception as exc:
        controlled_reasons = {
            "prompt_injection", "no_eligible_prompt_model", "tenant_context_unavailable",
            "unsupported_language", "unknown_or_inactive_response_type",
            "approved_fact_missing_or_cross_tenant", "approved_facts_do_not_match_template",
            "invalid_approved_template", "tenant_rendering_unavailable",
        }
        reason = str(exc) if str(exc) in controlled_reasons else "provider_or_schema_failure"
        value = _safe_default(reason)
        rendered_text = ""
        safety.update({"allowed": False, "reason": reason, "error_type": type(exc).__name__})
    latency_ms = int((time.monotonic() - start) * 1000)
    result = DecisionResult(value, audit_id, registry.id if registry else None, registry.prompt_version if registry else "unconfigured", registry.model_route if registry else "unconfigured", registry.model_name if registry else "unconfigured", registry.schema_version if registry else SCHEMA_VERSION, context, [{k: v for k, v in ref.items() if k != "content"} for ref in refs], safety, latency_ms, max(1, len(context) // 4) if context else 0, rendered_text)
    outcome = "evaluated" if value.decision == "REPLY" else ("escalated" if value.decision == "ESCALATE" else value.decision.lower())
    _update_audit(audit_id, client_id, lead_id, result=result, outcome=outcome)
    _refresh_summary(client_id, lead_id)
    return result


def record_outcome(*, client_id: int, lead_id: int, result: DecisionResult, outcome: str) -> None:
    _update_audit(result.audit_id, client_id, lead_id, result=result, outcome=outcome)


def mark_audit_outcome_locked(session: Any, *, audit_id: int | None, client_id: int, outcome: str, reason: str | None = None) -> None:
    if audit_id is None:
        return
    row = session.query(WhatsAppAIDecisionAudit).filter_by(id=audit_id, client_id=client_id).with_for_update().one_or_none()
    if row is not None:
        row.final_outcome = outcome
        if reason and not row.escalation_reason:
            row.escalation_reason = reason
        row.updated_at = _utcnow()


def record_intent_outcome(*, audit_id: int | None, client_id: int, outcome: str, reason: str | None = None) -> None:
    if audit_id is None or database.SessionLocal is None:
        return
    with database.SessionLocal() as session:
        mark_audit_outcome_locked(session, audit_id=audit_id, client_id=client_id, outcome=outcome, reason=reason)
        session.commit()


def queue_reply(*, intent_id: int, client_id: int, lead_id: int, result: DecisionResult) -> None:
    if database.SessionLocal is None:
        raise RuntimeError("ai_audit_database_unavailable")
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client_id).with_for_update().one()
        audit = session.query(WhatsAppAIDecisionAudit).filter_by(id=result.audit_id, client_id=client_id, lead_id=lead_id).with_for_update().one()
        if intent.state != "generating" or result.value.decision != "REPLY" or not result.safety_results.get("allowed", False):
            raise RuntimeError("ai_reply_not_queueable")
        if not result.rendered_text:
            raise RuntimeError("ai_reply_missing_deterministic_render")
        intent.body = result.rendered_text
        intent.ai_decision_audit_id = audit.id
        audit.outbound_intent_id = intent_id
        audit.final_outcome = "queued"
        audit.updated_at = _utcnow()
        if session.query(Message).filter_by(outbound_intent_id=intent.id).one_or_none() is None:
            session.add(Message(
                lead_id=lead_id,
                direction="OUTBOUND",
                msg_type="text",
                body=result.rendered_text,
                status="pending",
                channel="whatsapp",
                outbound_intent_id=intent.id,
            ))
        session.commit()


def finalize_non_reply(*, intent_id: int, client_id: int, lead_id: int, result: DecisionResult) -> None:
    """Atomically bind and terminalize a non-REPLY intent and its sole audit."""
    if database.SessionLocal is None:
        raise RuntimeError("ai_audit_database_unavailable")
    if result.value.decision == "REPLY":
        raise RuntimeError("ai_reply_requires_queue")
    outcome = "escalated" if result.value.decision == "ESCALATE" else result.value.decision.lower()
    with database.SessionLocal() as session:
        intent = session.query(WhatsAppOutboundIntent).filter_by(
            id=intent_id, client_id=client_id
        ).with_for_update().one()
        audit = session.query(WhatsAppAIDecisionAudit).filter_by(
            id=result.audit_id, client_id=client_id, lead_id=lead_id
        ).with_for_update().one()
        if intent.state != "generating" or intent.body is not None:
            raise RuntimeError("ai_non_reply_intent_not_terminalizable")
        if intent.ai_decision_audit_id not in {None, audit.id}:
            raise RuntimeError("ai_non_reply_audit_mismatch")
        if audit.outbound_intent_id not in {None, intent.id} or audit.decision != result.value.decision:
            raise RuntimeError("ai_non_reply_audit_mismatch")
        intent.ai_decision_audit_id = audit.id
        intent.state = "blocked"
        intent.failure_category = "ai_non_reply"
        intent.failure_reason = result.value.decision.lower()
        audit.outbound_intent_id = intent.id
        audit.final_outcome = outcome
        audit.updated_at = _utcnow()
        session.commit()


def durable_reply_guard(session: Any, client: Client, lead: Lead | None, *, audit_id: int | None, intent_id: int, body: str) -> str | None:
    if lead is None or lead.is_human_takeover:
        return "human_takeover"
    if audit_id is None:
        return "ai_audit_missing"
    intent = session.query(WhatsAppOutboundIntent).filter_by(id=intent_id, client_id=client.id).with_for_update().one_or_none()
    if intent is None or intent.ai_decision_audit_id != audit_id or intent.body != body:
        return "ai_intent_audit_mismatch"
    audit = session.query(WhatsAppAIDecisionAudit).filter_by(id=audit_id, client_id=client.id, lead_id=lead.id).with_for_update().one_or_none()
    if audit is None or audit.outbound_intent_id != intent_id or audit.decision != "REPLY" or not audit.safety_results.get("allowed", False):
        return "ai_reply_not_validated"
    if audit.response_digest != hashlib.sha256(body.encode("utf-8")).hexdigest():
        return "ai_reply_body_mismatch"
    registry = session.query(WhatsAppAIPromptModel).filter_by(id=audit.registry_id, client_id=client.id, purpose=PURPOSE, schema_version=audit.schema_version, is_active=True, evaluation_status="approved").one_or_none()
    if registry is None or registry.evaluated_at is None or (registry.updated_at is not None and registry.evaluated_at < registry.updated_at):
        return "ai_registry_not_current"
    if (registry.prompt_version, registry.model_route, registry.model_name) != (audit.prompt_version, audit.model_route, audit.model_name):
        return "ai_registry_mismatch"
    safety = dict(audit.safety_results or {})
    if safety.get("deterministic_render") is not True:
        return "ai_deterministic_render_missing"
    try:
        rendered, selection, _refs = _render_approved_reply(
            session,
            client_id=client.id,
            registry=registry,
            response_type=str(safety.get("response_type", "")),
            language=str(safety.get("language", "")),
            fact_ids=tuple(safety.get("approved_fact_ids", [])),
        )
    except (TypeError, ValueError):
        return "ai_approved_content_not_current"
    if selection.get("template_id") != safety.get("template_id") or rendered != body:
        return "ai_approved_content_changed"
    return None


def final_send_guard(result: DecisionResult, intent_id: int) -> Callable[[Any, Client, Lead | None], str | None]:
    def guard(session: Any, client: Client, lead: Lead | None) -> str | None:
        return durable_reply_guard(session, client, lead, audit_id=result.audit_id, intent_id=intent_id, body=result.rendered_text)
    return guard

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clients import gemini_client as gemini_module
from app.core import database
from app.core.database import Base
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
    WhatsAppWebhookEvent,
)
from app.services import ai_decision, whatsapp_outbox
from app.services.guardrails import minimize_sensitive_text, scan_input


GOLDEN = json.loads((Path(__file__).parents[1] / "fixtures" / "phase9_golden_conversations.json").read_text())


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


@pytest.fixture
def ai_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [
        Client.__table__,
        Lead.__table__,
        WhatsAppAIPromptModel.__table__,
        WhatsAppAIApprovedFact.__table__,
        WhatsAppAIResponseTemplate.__table__,
        WhatsAppConversationSummary.__table__,
        WhatsAppAIDecisionAudit.__table__,
        WhatsAppWebhookEvent.__table__,
        WhatsAppOutboundIntent.__table__,
        Message.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add(Client(id=1, name="Synthetic Tenant", is_active=True))
        session.add(Lead(id=1, client_id=1, phone="15550000001", name="Synthetic Person", business_name="Synthetic Clinic", status="Contacted"))
        session.add(WhatsAppAIPromptModel(
            id=1,
            client_id=1,
            purpose="whatsapp_reply",
            prompt_version="golden-v2",
            prompt_body="Reply professionally using only cited tenant knowledge.",
            model_route="ninerouter",
            model_name="offline-model",
            schema_version="v2",
            allowed_languages=["en"],
            tone="professional",
            evaluation_status="approved",
            evaluated_at=now,
            is_active=True,
            created_at=now,
            updated_at=now,
        ))
        session.add_all([
            WhatsAppAIApprovedFact(id=101, client_id=1, fact_key="booking_subject", fact_value="preferred booking time", source_reference="tenant:booking", is_active=True),
            WhatsAppAIApprovedFact(id=102, client_id=1, fact_key="approved_price", fact_value="₹999", source_reference="tenant:price", is_active=True),
            WhatsAppAIResponseTemplate(id=201, client_id=1, response_type="general_help", language="en", template_body="How can we help with your request?", required_fact_keys=[], is_active=True),
            WhatsAppAIResponseTemplate(id=202, client_id=1, response_type="booking_request", language="en", template_body="Please tell us your {booking_subject}.", required_fact_keys=["booking_subject"], is_active=True),
            WhatsAppAIResponseTemplate(id=203, client_id=1, response_type="pricing", language="en", template_body="Our approved price is {approved_price}.", required_fact_keys=["approved_price"], is_active=True),
        ])
        session.commit()
    monkeypatch.setattr(ai_decision, "retrieve_references", lambda *_args, **_kwargs: [])
    yield factory
    engine.dispose()


class FakeModel:
    def __init__(self, response: Any):
        self.response = response
        self.calls: list[dict[str, str]] = []

    def generate_structured_decision(self, _prompt, _context, **kwargs):
        self.calls.append(kwargs)
        if self.response == "__provider_failure__":
            raise RuntimeError("offline provider failure")
        return self.response


def _create_intent(factory, suffix: str) -> int:
    with factory() as session:
        event = WhatsAppWebhookEvent(client_id=1, event_kind="message", event_id=f"event-{suffix}", correlation_id=f"corr-{suffix}", phone_number_id="phone-1", payload={}, state="processed")
        session.add(event)
        session.flush()
        intent = WhatsAppOutboundIntent(client_id=1, inbound_event_id=event.id, reply_version=1, recipient_phone="15550000001", state="generating", correlation_id=f"corr-{suffix}")
        session.add(intent)
        session.commit()
        return intent.id


def test_strict_schema_rejects_extra_missing_boolean_and_wrong_types():
    valid = {"decision": "REPLY", "intent": "general", "response_type": "general_help", "approved_fact_ids": [], "language": "en", "confidence": 0.9, "escalation_reason": None}
    assert ai_decision._parse(valid).decision == "REPLY"
    invalid = [
        {**valid, "extra": "not allowed"},
        {key: value for key, value in valid.items() if key != "response_type"},
        {**valid, "confidence": True},
        {**valid, "intent": 7},
        {**valid, "approved_fact_ids": [True]},
        {**valid, "approved_fact_ids": [101, 101]},
        {**valid, "response_text": "must not exist"},
        {**valid, "decision": "WAIT"},
    ]
    for raw in invalid:
        with pytest.raises(ValueError):
            ai_decision._parse(raw)


def test_production_path_golden_gate_uses_evaluator_audit_and_sendability(ai_db):
    for index, case in enumerate(GOLDEN):
        fake = FakeModel(case["response"])
        if not scan_input(case["input"])[0]:
            result = ai_decision.reject_input(client_id=1, lead_id=1, correlation_id=f"golden-{index}")
        else:
            result = ai_decision.evaluate(client_id=1, lead_id=1, inbound_text=case["input"], gemini=fake, correlation_id=f"golden-{index}")
        assert result.value.decision == case["expected"], case["name"]
        sendable = False
        if result.value.decision == "REPLY":
            intent_id = _create_intent(ai_db, str(index))
            ai_decision.queue_reply(intent_id=intent_id, client_id=1, lead_id=1, result=result)
            with ai_db() as session:
                lead = session.get(Lead, 1)
                if case["name"] == "takeover_race":
                    lead.is_human_takeover = True
                    session.flush()
                sendable = ai_decision.final_send_guard(result, intent_id)(session, session.get(Client, 1), lead) is None
                if case["name"] == "booking_request":
                    assert session.get(WhatsAppOutboundIntent, intent_id).body == "Please tell us your preferred booking time."
        assert sendable is case["sendable"], case["name"]
        with ai_db() as session:
            rows = session.query(WhatsAppAIDecisionAudit).filter_by(id=result.audit_id).all()
            assert len(rows) == 1
            assert all("content" not in ref for ref in rows[0].retrieval_references)
            assert not hasattr(rows[0], "response_text")
        if case["name"] == "prompt_injection":
            assert fake.calls == []
        elif fake.calls:
            assert fake.calls[0]["provider_route"] == "ninerouter"
            assert fake.calls[0]["model_name"] == "offline-model"


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"response_type": "customer_count", "approved_fact_ids": []}, "unknown_or_inactive_response_type"),
        ({"response_type": "availability", "approved_fact_ids": []}, "unknown_or_inactive_response_type"),
        ({"response_type": "pricing", "approved_fact_ids": [9999]}, "approved_fact_missing_or_cross_tenant"),
        ({"response_type": "pricing", "approved_fact_ids": []}, "approved_facts_do_not_match_template"),
        ({"response_type": "general_help", "approved_fact_ids": [], "language": "fr"}, "unsupported_language"),
    ],
)
def test_production_evaluator_rejects_unapproved_reply_selection(ai_db, override, expected_reason):
    response = {
        "decision": "REPLY",
        "intent": "general",
        "response_type": "general_help",
        "approved_fact_ids": [],
        "language": "en",
        "confidence": 0.95,
        "escalation_reason": None,
    }
    response.update(override)
    fake = FakeModel(response)

    result = ai_decision.evaluate(
        client_id=1,
        lead_id=1,
        inbound_text="Please help with my request",
        gemini=fake,
        correlation_id=f"safety-{expected_reason}",
    )

    assert result.value.decision == "ESCALATE"
    assert result.value.escalation_reason == expected_reason
    assert result.safety_results["allowed"] is False
    assert result.safety_results["reason"] == expected_reason
    with ai_db() as session:
        audit = session.get(WhatsAppAIDecisionAudit, result.audit_id)
        assert audit.decision == "ESCALATE"
        assert audit.final_outcome == "escalated"
        assert audit.safety_results["reason"] == expected_reason


@pytest.mark.parametrize("free_form", [
    "We have served 500 customers.",
    "We are available everywhere.",
    "Do you understand anything?",
])
def test_free_form_model_text_never_enters_automatic_send_path(ai_db, free_form):
    response = {"decision": "REPLY", "intent": "general", "response_type": "general_help", "approved_fact_ids": [], "language": "en", "confidence": .95, "escalation_reason": None, "response_text": free_form}
    result = ai_decision.evaluate(client_id=1, lead_id=1, inbound_text="Help", gemini=FakeModel(response), correlation_id="free-form-" + str(len(free_form)))
    assert result.value.decision == "ESCALATE"
    assert result.rendered_text == ""
    assert result.safety_results["reason"] == "provider_or_schema_failure"


def test_cross_tenant_fact_id_cannot_render_or_queue(ai_db):
    with ai_db() as session:
        session.add(Client(id=2, name="Other Synthetic Tenant", is_active=True))
        session.add(WhatsAppAIApprovedFact(id=777, client_id=2, fact_key="approved_price", fact_value="invented", is_active=True))
        session.commit()
    response = {"decision": "REPLY", "intent": "pricing", "response_type": "pricing", "approved_fact_ids": [777], "language": "en", "confidence": .95, "escalation_reason": None}
    result = ai_decision.evaluate(client_id=1, lead_id=1, inbound_text="Price?", gemini=FakeModel(response), correlation_id="cross-tenant-fact")
    assert result.value.decision == "ESCALATE"
    assert result.value.escalation_reason == "approved_fact_missing_or_cross_tenant"
    assert result.rendered_text == ""


def test_reply_retry_reuses_audit_without_second_model_call_or_body(ai_db):
    intent_id = _create_intent(ai_db, "reply-retry")
    response = {"decision": "REPLY", "intent": "booking", "response_type": "booking_request", "approved_fact_ids": [101], "language": "en", "confidence": .95, "escalation_reason": None}
    first_model = FakeModel(response)
    first = ai_decision.evaluate(client_id=1, lead_id=1, inbound_text="Book", gemini=first_model, correlation_id="reply-retry", intent_id=intent_id)
    retry_model = FakeModel(response)
    resumed = ai_decision.evaluate(client_id=1, lead_id=1, inbound_text="Book", gemini=retry_model, correlation_id="reply-retry", intent_id=intent_id)
    assert first.value.decision == "REPLY"
    assert first.rendered_text == "Please tell us your preferred booking time."
    assert resumed.audit_id == first.audit_id
    assert resumed.value.decision == "ESCALATE"
    assert retry_model.calls == []
    assert resumed.rendered_text == ""
    with ai_db() as session:
        assert session.query(WhatsAppAIDecisionAudit).filter_by(correlation_id="reply-retry").count() == 1
        assert session.get(WhatsAppOutboundIntent, intent_id).body is None


@pytest.mark.parametrize("decision", ["WAIT", "ESCALATE", "STOP", "NO_ACTION"])
def test_non_reply_intent_is_terminal_and_replay_reuses_audit_lifecycle(ai_db, decision):
    response = {
        "decision": decision,
        "intent": "offline-test",
        "response_type": None,
        "approved_fact_ids": [],
        "language": "",
        "confidence": 0.9,
        "escalation_reason": None,
    }
    if decision == "ESCALATE":
        response["escalation_reason"] = "human_review"
    intent_id = _create_intent(ai_db, f"non-reply-{decision}")
    first_model = FakeModel(response)
    result = ai_decision.evaluate(
        client_id=1,
        lead_id=1,
        inbound_text="Please help with my request",
        gemini=first_model,
        correlation_id=f"non-reply-{decision}",
        intent_id=intent_id,
    )
    resumed_model = FakeModel(response)
    resumed = ai_decision.evaluate(
        client_id=1,
        lead_id=1,
        inbound_text="Please help with my request",
        gemini=resumed_model,
        correlation_id=f"non-reply-{decision}",
        intent_id=intent_id,
    )

    ai_decision.finalize_non_reply(
        intent_id=intent_id,
        client_id=1,
        lead_id=1,
        result=resumed,
    )

    assert resumed.audit_id == result.audit_id
    assert resumed.value.decision == decision
    assert resumed_model.calls == []
    assert whatsapp_outbox.claim_for_generation(intent_id=intent_id, client_id=1) == "skip"
    with ai_db() as session:
        intent = session.get(WhatsAppOutboundIntent, intent_id)
        audit = session.get(WhatsAppAIDecisionAudit, result.audit_id)
        assert intent.state == "blocked"
        assert intent.ai_decision_audit_id == result.audit_id
        assert audit.outbound_intent_id == intent_id
        assert session.query(WhatsAppAIDecisionAudit).filter_by(correlation_id=f"non-reply-{decision}").count() == 1


def test_crash_before_model_completion_resumes_same_audit_without_regeneration(ai_db):
    intent_id = _create_intent(ai_db, "crash-before-completion")
    registry = ai_decision._registry_for_attempt(1)
    audit_id, created = ai_decision._bind_or_resume_audit(
        client_id=1,
        lead_id=1,
        correlation_id="crash-before-completion",
        registry=registry,
        intent_id=intent_id,
    )
    assert created is True
    model = FakeModel({"decision": "WAIT", "intent": "x", "response_type": None, "approved_fact_ids": [], "language": "", "confidence": 0.9, "escalation_reason": None})

    resumed = ai_decision.evaluate(
        client_id=1,
        lead_id=1,
        inbound_text="Please help with my request",
        gemini=model,
        correlation_id="crash-before-completion",
        intent_id=intent_id,
    )
    ai_decision.finalize_non_reply(intent_id=intent_id, client_id=1, lead_id=1, result=resumed)

    assert resumed.audit_id == audit_id
    assert resumed.value.decision == "ESCALATE"
    assert resumed.value.escalation_reason == "interrupted_attempt"
    assert model.calls == []
    assert whatsapp_outbox.claim_for_generation(intent_id=intent_id, client_id=1) == "skip"
    with ai_db() as session:
        assert session.query(WhatsAppAIDecisionAudit).filter_by(correlation_id="crash-before-completion").count() == 1


def test_context_summary_and_audit_minimise_sensitive_data(ai_db):
    secret = "Synthetic Person email person@example.com phone +91 98765 43210 account id ACCT-7788 at 12 Market Road"
    with ai_db() as session:
        session.add(Message(lead_id=1, direction="INBOUND", channel="whatsapp", msg_type="text", body=secret))
        session.commit()
    context, _ = ai_decision.build_context(1, 1, secret)
    for value in ("Synthetic Person", "person@example.com", "98765", "ACCT-7788", "12 Market Road"):
        assert value not in context
    assert context.count("[REDACTED]") >= 4
    ai_decision._refresh_summary(1, 1)
    with ai_db() as session:
        summary = session.query(WhatsAppConversationSummary).filter_by(client_id=1, lead_id=1).one().summary
    assert "person@example.com" not in summary and "Synthetic Person" not in summary
    assert "[REDACTED]" in minimize_sensitive_text(secret, known_names=["Synthetic Person"])


def test_context_and_token_estimate_remain_bounded_with_approved_catalog(ai_db):
    with ai_db() as session:
        session.add(WhatsAppConversationSummary(client_id=1, lead_id=1, summary="summary " * 1000))
        session.add_all([
            Message(lead_id=1, direction="INBOUND", channel="whatsapp", msg_type="text", body="message " * 1000)
            for _ in range(20)
        ])
        session.commit()
    response = {"decision": "REPLY", "intent": "general", "response_type": "general_help", "approved_fact_ids": [], "language": "en", "confidence": .95, "escalation_reason": None}
    result = ai_decision.evaluate(client_id=1, lead_id=1, inbound_text="Help", gemini=FakeModel(response), correlation_id="bounded-context")
    assert len(result.context_text) <= ai_decision.MAX_CONTEXT_CHARS
    assert "APPROVED_RESPONSE_TYPES" in result.context_text
    assert "TYPE=general_help" in result.context_text
    assert "FACT id=101" in result.context_text
    assert result.token_estimate <= ai_decision.MAX_CONTEXT_CHARS // 4


def test_missing_or_cross_tenant_registry_fails_closed(ai_db):
    with ai_db() as session:
        session.add(Client(id=2, name="Other Tenant", is_active=True))
        session.add(Lead(id=2, client_id=2, phone="15550000002", status="Contacted"))
        session.commit()
    fake = FakeModel({"decision": "REPLY", "intent": "x", "response_type": "general_help", "approved_fact_ids": [], "language": "en", "confidence": 1, "escalation_reason": None})
    result = ai_decision.evaluate(client_id=2, lead_id=2, inbound_text="Hello", gemini=fake, correlation_id="cross-tenant")
    assert result.value.decision == "ESCALATE"
    assert result.value.escalation_reason == "no_eligible_prompt_model"
    assert fake.calls == []


def test_registry_enforces_one_active_row_per_tenant_purpose_schema(ai_db):
    now = datetime.now(timezone.utc)
    with ai_db() as session:
        session.add(WhatsAppAIPromptModel(client_id=1, purpose="whatsapp_reply", prompt_version="duplicate-active", prompt_body="approved", model_route="ninerouter", model_name="offline-model", schema_version="v2", allowed_languages=["en"], tone="professional", evaluation_status="approved", evaluated_at=now, is_active=True, created_at=now, updated_at=now))
        with pytest.raises(IntegrityError):
            session.commit()


def test_registry_route_and_model_control_actual_client_call(monkeypatch):
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            content = json.dumps({"decision": "WAIT", "intent": "test", "response_type": None, "approved_fact_ids": [], "language": "", "confidence": 0.8, "escalation_reason": None})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(gemini_module, "_router_client", SimpleNamespace(chat=SimpleNamespace(completions=Completions())))
    client = object.__new__(gemini_module.GeminiClient)
    result = client.generate_structured_decision("prompt", "context", schema_version="v2", provider_route="ninerouter", model_name=gemini_module.NINEROUTER_MODEL)
    assert result["decision"] == "WAIT"
    assert calls[0]["model"] == gemini_module.NINEROUTER_MODEL
    with pytest.raises(ValueError):
        client.generate_structured_decision("prompt", "context", schema_version="v2", provider_route="ninerouter", model_name="unevaluated-model")

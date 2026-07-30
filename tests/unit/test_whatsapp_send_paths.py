from __future__ import annotations

import ast
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import main as application
from app.api.routers import whatsapp as legacy_whatsapp
from app.services import jobs
from scripts import send_initial_outreach


def _sent_result(message_id: str = "wamid.policy") -> SimpleNamespace:
    return SimpleNamespace(
        state="sent",
        reason_code="allowed",
        provider_message_id=message_id,
    )


def test_only_low_level_client_can_call_meta_send_methods():
    root = Path(__file__).resolve().parents[2]
    low_level_client = (
        root / "app" / "clients" / "whatsapp_client.py"
    ).resolve()
    paths = [
        *root.joinpath("app").rglob("*.py"),
        root / "main.py",
        *root.joinpath("scripts").rglob("*.py"),
        *root.joinpath("debug").rglob("*.py"),
    ]
    violations = []
    for path in paths:
        if path.resolve() == low_level_client:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        whatsapp_receivers = {"whatsapp"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    if imported.name == "whatsapp":
                        whatsapp_receivers.add(
                            imported.asname or imported.name
                        )
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "WhatsAppClient"
            ):
                whatsapp_receivers.update(
                    target.id
                    for target in node.targets
                    if isinstance(target, ast.Name)
                )
        for node in ast.walk(tree):
            receiver = node.func.value if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
            ) else None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"send_message", "send_template"}
                and (
                    isinstance(receiver, ast.Name)
                    and receiver.id in whatsapp_receivers
                    or isinstance(receiver, ast.Call)
                    and isinstance(receiver.func, ast.Name)
                    and receiver.func.id == "WhatsAppClient"
                    or isinstance(receiver, ast.Attribute)
                    and receiver.attr == "whatsapp"
                )
            ):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert violations == []


def test_follow_up_scheduler_executes_template_policy(monkeypatch):
    policy_calls: list[dict[str, Any]] = []

    def record_policy_call(**kwargs: Any) -> SimpleNamespace:
        policy_calls.append(kwargs)
        return _sent_result()

    timestamp = (
        datetime.now() - timedelta(hours=49)
    ).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(
        application.store,
        "get_contacted_leads",
        lambda _client_id: [
            {
                "fields": {
                    "Last_Message": f"[{timestamp}] INBOUND (text): hello",
                    "Phone number type": "15550000001",
                }
            }
        ],
    )
    monkeypatch.setattr(
        application.whatsapp_policy,
        "send_immediate_template",
        record_policy_call,
    )
    monkeypatch.setattr(
        application.store,
        "append_message",
        lambda *_args, **_kwargs: True,
    )

    application._process_followups_for_client(7, "follow_up")

    assert policy_calls[0]["action"] == "follow_up_template_send"
    assert policy_calls[0]["client_id"] == 7
    assert policy_calls[0]["template_name"] == "follow_up"


def test_booking_alert_executes_tenant_operator_template_policy(monkeypatch):
    policy_calls: list[dict[str, Any]] = []

    def record_policy_call(**kwargs: Any) -> SimpleNamespace:
        policy_calls.append(kwargs)
        return _sent_result()

    monkeypatch.setattr(
        application.calendly,
        "get_recent_bookings",
        lambda: [
            {
                "phone": "15550000001",
                "name": "Ada",
                "start_time": "2026-08-01T10:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        application.store,
        "get_lead",
        lambda *_args, **_kwargs: {
            "fields": {"client_id": 7, "Status": "Contacted"}
        },
    )
    monkeypatch.setattr(
        application.store,
        "update_lead_status",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        application.store,
        "append_message",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        application.whatsapp_policy,
        "get_operator_template",
        lambda **_kwargs: application.whatsapp_policy.OperatorTemplate(
            phone="15550000999",
            name="booking_alert",
            language="en",
        ),
    )
    monkeypatch.setattr(
        application.whatsapp_policy,
        "send_immediate_template",
        record_policy_call,
    )

    application.calendly_sync_job()

    assert policy_calls[0]["action"] == "booking_alert_send"
    assert policy_calls[0]["client_id"] == 7
    assert policy_calls[0]["phone"] == "15550000999"
    assert policy_calls[0]["recipient_kind"] == "operator"


def test_live_outreach_script_executes_template_policy(monkeypatch):
    policy_calls: list[dict[str, Any]] = []
    status_updates = []

    def record_policy_call(**kwargs: Any) -> SimpleNamespace:
        policy_calls.append(kwargs)
        return _sent_result()

    class FakeAirtable:
        def _search(self, *_args, **_kwargs):
            return [
                {
                    "fields": {
                        "Phone number type": "15550000001",
                        "Name": "Ada",
                    }
                }
            ]

        def update_lead_status(self, *args, **kwargs):
            status_updates.append((args, kwargs))

    monkeypatch.setattr(
        send_initial_outreach,
        "AirtableClient",
        FakeAirtable,
    )
    monkeypatch.setattr(
        send_initial_outreach,
        "WhatsAppClient",
        lambda: SimpleNamespace(send_template=lambda *_a, **_k: None),
    )
    monkeypatch.setattr(
        send_initial_outreach.whatsapp_policy,
        "send_immediate_template",
        record_policy_call,
    )

    send_initial_outreach.main(live=True)

    assert policy_calls[0]["action"] == "local_initial_outreach_send"
    assert status_updates[0][0] == ("15550000001", "Contacted")


@pytest.mark.parametrize(
    ("safe", "refusal", "expected_action"),
    [
        (False, "Safe refusal", "guardrail_refusal_send"),
        (True, None, "legacy_ai_reply_send"),
    ],
)
def test_worker_immediate_paths_execute_policy(
    monkeypatch,
    safe,
    refusal,
    expected_action,
):
    policy_calls: list[dict[str, Any]] = []

    def record_policy_call(**kwargs: Any) -> SimpleNamespace:
        policy_calls.append(kwargs)
        return _sent_result()

    context = SimpleNamespace(
        client=SimpleNamespace(id=7),
        gemini=SimpleNamespace(
            _system_prompt="offline",
            parse_conversation_history=lambda _text: [],
            generate_response_with_history=lambda *_args: "AI reply",
        ),
        won_stages=["Booked"],
        lost_stages=["Lost"],
    )
    store = MagicMock()
    store.get_lead.return_value = {
        "id": "lead-1",
        "fields": {
            "Status": "Contacted",
            "Last_Message": "",
            "Name": "Ada",
            "is_human_takeover": False,
        },
    }
    store.append_message.return_value = True
    monkeypatch.setattr(jobs, "get_store", lambda: store)
    monkeypatch.setattr(
        jobs.tenant,
        "resolve_context_by_phone_id",
        lambda _phone_id: context,
    )
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "record_inbound_opt_out",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "preflight_text",
        lambda **_kwargs: SimpleNamespace(
            allowed=True,
            reason_code="allowed",
        ),
    )
    monkeypatch.setattr(jobs, "check_limit", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(jobs, "retrieve_context", lambda *_a, **_k: [])
    monkeypatch.setattr(jobs, "scan_input", lambda _text: (safe, refusal))
    monkeypatch.setattr(jobs, "score_confidence", lambda *_a, **_k: 1.0)
    monkeypatch.setattr(jobs, "_run_analytics", lambda *_a, **_k: None)
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "send_immediate_text",
        record_policy_call,
    )

    jobs.process_webhook_message(
        "tenant-phone-id",
        {
            "id": f"wamid.{expected_action}",
            "from": "919999999999",
            "type": "text",
            "text": {"body": "offline input"},
        },
        current_client_id=7,
    )

    assert policy_calls[0]["action"] == expected_action
    assert policy_calls[0]["client_id"] == 7


def test_worker_outbox_path_executes_final_policy_dispatch(monkeypatch):
    dispatch_calls: list[dict[str, Any]] = []

    def record_dispatch_call(**kwargs: Any) -> SimpleNamespace:
        dispatch_calls.append(kwargs)
        return SimpleNamespace(
            state="sent",
            newly_sent=True,
            provider_message_id="wamid.outbox",
        )

    context = SimpleNamespace(
        client=SimpleNamespace(id=7),
        gemini=SimpleNamespace(
            _system_prompt="offline",
            parse_conversation_history=lambda _text: [],
            generate_response_with_history=lambda *_args: "AI reply",
        ),
        won_stages=["Booked"],
        lost_stages=["Lost"],
    )
    store = MagicMock()
    store.get_lead.return_value = {
        "id": "lead-1",
        "fields": {
            "Status": "Contacted",
            "Last_Message": "",
            "Name": "Ada",
            "is_human_takeover": False,
        },
    }
    store.append_message.return_value = True
    monkeypatch.setattr(jobs, "get_store", lambda: store)
    monkeypatch.setattr(
        jobs.tenant,
        "resolve_context_by_phone_id",
        lambda _phone_id: context,
    )
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "record_inbound_opt_out",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "preflight_text",
        lambda **_kwargs: SimpleNamespace(
            allowed=True,
            reason_code="allowed",
        ),
    )
    monkeypatch.setattr(jobs, "check_limit", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(jobs, "retrieve_context", lambda *_a, **_k: [])
    monkeypatch.setattr(jobs, "scan_input", lambda _text: (True, None))
    monkeypatch.setattr(jobs, "score_confidence", lambda *_a, **_k: 1.0)
    monkeypatch.setattr(jobs, "_run_analytics", lambda *_a, **_k: None)
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "create_or_get_intent",
        lambda **_kwargs: 41,
    )
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "claim_for_generation",
        lambda **_kwargs: "generate",
    )
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "set_generated_body",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        jobs.whatsapp_outbox,
        "dispatch_intent",
        record_dispatch_call,
    )

    jobs.process_webhook_message(
        "tenant-phone-id",
        {
            "id": "wamid.worker.outbox",
            "from": "919999999999",
            "type": "text",
            "text": {"body": "offline input"},
        },
        current_client_id=7,
        inbound_event_id="wamid.worker.outbox",
    )

    assert dispatch_calls[0]["intent_id"] == 41
    assert dispatch_calls[0]["client_id"] == 7


def test_current_and_legacy_hot_lead_alerts_execute_policy(monkeypatch):
    modern_calls: list[dict[str, Any]] = []
    legacy_calls: list[dict[str, Any]] = []

    def record_modern_call(**kwargs: Any) -> SimpleNamespace:
        modern_calls.append(kwargs)
        return _sent_result()

    def record_legacy_call(**kwargs: Any) -> SimpleNamespace:
        legacy_calls.append(kwargs)
        return _sent_result()

    alert = jobs.whatsapp_policy.OperatorTemplate(
        phone="15550000999",
        name="hot_alert",
        language="en",
    )
    client = SimpleNamespace(hot_lead_threshold=80)
    lead = SimpleNamespace(
        notified_hot_at=None,
        lead_score=None,
        lead_score_numeric=None,
        status="Contacted",
    )

    class Query:
        def __init__(self, entity):
            self.entity = entity

        def filter(self, *_args):
            return self

        def first(self):
            return client if self.entity is jobs.Client else lead

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, entity):
            return Query(entity)

        def commit(self):
            return None

    monkeypatch.setattr(jobs.database, "SessionLocal", Session)
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "get_operator_template",
        lambda **_kwargs: alert,
    )
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "send_immediate_template",
        record_modern_call,
    )
    jobs._run_analytics(
        MagicMock(),
        "15550000001",
        "history",
        "interested",
        "Ada",
        SimpleNamespace(
            extract_lead_info=lambda _text: None,
            score_lead=lambda _text: {"score": 90},
        ),
        ["Booked"],
        ["Lost"],
        7,
    )

    fake_store = MagicMock()
    monkeypatch.setattr(
        "app.store.store.get_store",
        lambda: fake_store,
    )

    class FakeGemini:
        def __init__(self, **_kwargs):
            pass

        def score_lead(self, _text):
            return "Hot"

        def extract_lead_info(self, _text):
            return None

    monkeypatch.setattr(
        "app.clients.gemini_client.GeminiClient",
        FakeGemini,
    )
    monkeypatch.setattr(
        legacy_whatsapp.whatsapp_policy,
        "get_operator_template",
        lambda **_kwargs: alert,
    )
    monkeypatch.setattr(
        legacy_whatsapp.whatsapp_policy,
        "send_immediate_template",
        record_legacy_call,
    )
    legacy_whatsapp._process_analytics_and_extraction_bg(
        "15550000001",
        "history",
        "interested",
        "Ada",
        "system",
        None,
        ["Hot"],
        ["Lost"],
        7,
        None,
    )

    assert modern_calls[0]["action"] == "hot_lead_alert_send"
    assert modern_calls[0]["phone"] == "15550000999"
    assert legacy_calls[0]["action"] == "legacy_hot_lead_alert_send"
    assert legacy_calls[0]["phone"] == "15550000999"


@pytest.mark.parametrize(
    "filename",
    [
        "profile_webhook.py",
        "send_prod_webhook.py",
    ],
)
def test_debug_send_utilities_are_not_executable_in_production(filename):
    root = Path(__file__).resolve().parents[2]
    path = root / "debug" / filename
    spec = importlib.util.spec_from_file_location(
        f"disabled_{path.stem}",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit, match="Disabled"):
        spec.loader.exec_module(module)

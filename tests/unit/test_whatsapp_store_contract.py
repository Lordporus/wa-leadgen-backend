import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from sqlalchemy import UniqueConstraint, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.airtable_client import AirtableClient
from app.services import jobs
from app.core.database import Base
from app.core.models import Client, Lead
from app.store.db_client import DatabaseClient
from app.store.store import DualWriteStore
from app.store.webhook_store import WebhookStore


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _dual_store():
    record: dict[str, Any] = {
        "id": "recOffline",
        "fields": {
            "Name": "Offline Lead",
            "Phone number type": "919999999999",
            "Status": "New Lead",
            "Last_Message": "",
            "is_human_takeover": True,
        },
    }
    primary = MagicMock()
    primary.get_lead.return_value = record
    primary.add_lead.return_value = record
    primary.update_lead_status.return_value = record
    primary.update_lead_status_by_id.return_value = record
    primary.append_message.return_value = True
    secondary = MagicMock()
    secondary_record: dict[str, Any] = {
        "id": "42",
        "fields": {
            **record["fields"],
            "is_human_takeover": record["fields"]["is_human_takeover"],
        },
    }
    secondary.get_lead.return_value = secondary_record

    def update_primary_takeover(record_id, enabled, client_id):
        if record_id != record["id"] or client_id != 7:
            return None
        record["fields"]["is_human_takeover"] = enabled
        return record

    def update_secondary_takeover(record_id, enabled, client_id):
        if str(record_id) != secondary_record["id"] or client_id != 7:
            return None
        secondary_record["fields"]["is_human_takeover"] = enabled
        return secondary_record

    primary.update_human_takeover_by_id.side_effect = update_primary_takeover
    secondary.update_human_takeover_by_id.side_effect = update_secondary_takeover
    return DualWriteStore(primary, secondary), primary, secondary


def test_dual_store_forwards_tenant_context_to_every_write():
    store, primary, secondary = _dual_store()

    assert store.add_lead(
        "Offline Lead",
        "919999999999",
        "Inbound WhatsApp",
        client_id=7,
    )
    assert store.update_lead_status(
        "919999999999",
        "Contacted",
        client_id=7,
    )
    assert store.update_lead_status_by_id(
        "recOffline",
        "Qualified",
        client_id=7,
    )
    assert store.update_human_takeover_by_id(
        "recOffline",
        True,
        client_id=7,
    )
    assert store.append_message(
        "919999999999",
        "inbound",
        "Offline body",
        "text",
        "wamid.offline",
        client_id=7,
    )
    store.update_message_status("wamid.offline", "delivered", client_id=7)
    store.update_lead_info(
        "919999999999",
        "Offline Lead",
        "Offline Business",
        client_id=7,
    )
    store.update_lead_score("919999999999", "Warm", client_id=7)

    primary.add_lead.assert_called_once_with(
        "Offline Lead",
        "919999999999",
        "Inbound WhatsApp",
        client_id=7,
    )
    secondary.add_lead.assert_called_once_with(
        "Offline Lead",
        "919999999999",
        "Inbound WhatsApp",
        client_id=7,
    )
    primary.update_lead_status.assert_called_once_with(
        "919999999999",
        "Contacted",
        client_id=7,
    )
    assert secondary.update_lead_status.call_args_list == [
        call("919999999999", "Contacted", client_id=7),
        call("919999999999", "Qualified", client_id=7),
    ]
    primary.update_human_takeover_by_id.assert_called_once_with(
        "recOffline",
        True,
        client_id=7,
    )
    secondary.get_lead.assert_called_once_with(
        "919999999999",
        client_id=7,
    )
    secondary.update_human_takeover_by_id.assert_called_once_with(
        "42",
        True,
        client_id=7,
    )
    primary.append_message.assert_called_once_with(
        "919999999999",
        "inbound",
        "Offline body",
        "text",
        "wamid.offline",
        client_id=7,
    )
    secondary.append_message.assert_called_once_with(
        "919999999999",
        "inbound",
        "Offline body",
        "text",
        "wamid.offline",
        client_id=7,
    )
    secondary.update_message_status.assert_called_once_with(
        "wamid.offline",
        "delivered",
        client_id=7,
    )
    primary.update_lead_info.assert_called_once_with(
        "919999999999",
        "Offline Lead",
        "Offline Business",
        client_id=7,
    )
    secondary.update_lead_info.assert_called_once_with(
        "919999999999",
        "Offline Lead",
        "Offline Business",
        client_id=7,
    )
    primary.update_lead_score.assert_called_once_with(
        "919999999999",
        "Warm",
        client_id=7,
    )
    secondary.update_lead_score.assert_called_once_with(
        "919999999999",
        "Warm",
        client_id=7,
    )


def test_concrete_store_methods_require_tenant_argument():
    methods_by_class = {
        AirtableClient: (
            "_search",
            "get_lead",
            "get_all_leads",
            "get_lead_by_id",
            "get_messages_for_lead",
            "add_lead",
            "update_lead_status",
            "update_lead_status_by_id",
            "update_human_takeover_by_id",
            "append_message",
            "update_message_status",
            "update_lead_info",
            "update_lead_score",
        ),
        DatabaseClient: (
            "_search",
            "get_lead",
            "get_all_leads",
            "get_lead_by_id",
            "get_messages_for_lead",
            "add_lead",
            "update_lead_status",
            "update_lead_status_by_id",
            "update_human_takeover_by_id",
            "append_message",
            "update_message_status",
            "update_lead_info",
            "update_lead_score",
        ),
        DualWriteStore: (
            "_search",
            "get_lead",
            "get_all_leads",
            "get_lead_by_id",
            "get_messages_for_lead",
            "add_lead",
            "update_lead_status",
            "update_lead_status_by_id",
            "update_human_takeover_by_id",
            "append_message",
            "update_message_status",
            "update_lead_info",
            "update_lead_score",
        ),
        WebhookStore: (
            "get_lead",
            "add_lead",
            "update_lead_status",
            "update_human_takeover_by_id",
            "append_message",
            "update_message_status",
            "update_lead_info",
            "update_lead_score",
        ),
    }

    for implementation, methods in methods_by_class.items():
        for method_name in methods:
            parameter = inspect.signature(
                getattr(implementation, method_name)
            ).parameters["client_id"]
            assert parameter.default is inspect.Parameter.empty, (
                f"{implementation.__name__}.{method_name} must require client_id"
            )


def test_application_workers_and_scripts_pass_tenant_context_to_store_calls():
    minimum_positional_args = {
        "_search": 2,
        "get_contacted_leads": 1,
        "get_lead": 2,
        "get_all_leads": 1,
        "get_lead_by_id": 2,
        "get_messages_for_lead": 2,
        "add_lead": 4,
        "update_lead_status": 3,
        "update_lead_status_by_id": 3,
        "update_human_takeover_by_id": 3,
        "append_message": 6,
        "update_message_status": 3,
        "update_lead_info": 4,
        "update_lead_score": 3,
    }
    keyword_only_tenant_methods = {"add_lead", "append_message"}
    violations = []
    paths = [
        BACKEND_ROOT / "main.py",
        *(BACKEND_ROOT / "app").rglob("*.py"),
        *(BACKEND_ROOT / "scripts").rglob("*.py"),
        *(BACKEND_ROOT / "debug").rglob("*.py"),
    ]

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method_name = node.func.attr
            if method_name not in minimum_positional_args:
                continue
            has_keyword = any(
                keyword.arg == "client_id"
                for keyword in node.keywords
            )
            missing_tenant = not has_keyword and (
                method_name in keyword_only_tenant_methods
                or len(node.args) < minimum_positional_args[method_name]
            )
            if missing_tenant:
                violations.append(
                    f"{path.relative_to(BACKEND_ROOT)}:{node.lineno} {method_name}"
                )

    assert violations == []


def test_dual_store_missing_tenant_raises_before_adapter_calls():
    store, primary, secondary = _dual_store()

    missing_tenant_calls = (
        lambda: store.add_lead("Lead", "919999999999"),
        lambda: store.update_lead_status("919999999999", "Contacted"),
        lambda: store.update_human_takeover_by_id("recOffline", True),
        lambda: store.append_message(
            "919999999999",
            "inbound",
            "Offline body",
        ),
        lambda: store.update_message_status("wamid.offline", "read"),
        lambda: store.update_lead_info("919999999999", "Lead", None),
        lambda: store.update_lead_score("919999999999", "Warm"),
    )
    for call_without_tenant in missing_tenant_calls:
        with pytest.raises(TypeError):
            call_without_tenant()

    primary.add_lead.assert_not_called()
    primary.update_lead_status.assert_not_called()
    primary.append_message.assert_not_called()
    primary.update_lead_info.assert_not_called()
    primary.update_lead_score.assert_not_called()
    secondary.assert_not_called()


def test_dual_takeover_state_is_visible_to_worker_read_primary(monkeypatch):
    store, primary, secondary = _dual_store()
    primary.get_lead.return_value["fields"]["is_human_takeover"] = False

    updated = store.update_human_takeover_by_id(
        "recOffline",
        True,
        client_id=7,
    )
    assert updated["fields"]["is_human_takeover"] is True

    context = SimpleNamespace(
        client=SimpleNamespace(id=7),
        gemini=MagicMock(),
        won_stages=["Booked"],
        lost_stages=["Lost"],
    )
    monkeypatch.setattr(jobs, "get_store", lambda: store)
    monkeypatch.setattr(
        jobs.tenant,
        "resolve_context_by_phone_id",
        lambda phone_number_id: context,
    )

    jobs.process_webhook_message(
        "offline-phone-number-id",
        {
            "id": "wamid.after-takeover",
            "from": "919999999999",
            "type": "text",
            "text": {"body": "A human is handling this"},
        },
    )

    context.gemini.generate_response_with_history.assert_not_called()
    assert primary.get_lead.return_value["fields"]["is_human_takeover"] is True
    assert (
        secondary.update_human_takeover_by_id.call_args_list[-1]
        == call("42", True, client_id=7)
    )


def test_webhook_worker_uses_tenant_scoped_dual_store_signatures(monkeypatch):
    store, primary, secondary = _dual_store()
    context = SimpleNamespace(
        client=SimpleNamespace(id=7),
        gemini=MagicMock(),
        won_stages=["Booked"],
        lost_stages=["Lost"],
    )
    monkeypatch.setattr(jobs, "get_store", lambda: store)
    monkeypatch.setattr(
        jobs.tenant,
        "resolve_context_by_phone_id",
        lambda phone_number_id: context,
    )

    jobs.process_webhook_message(
        "offline-phone-number-id",
        {
            "id": "wamid.inbound",
            "from": "919999999999",
            "type": "text",
            "text": {"body": "Offline hello"},
        },
    )

    primary.get_lead.assert_called_once_with("919999999999", client_id=7)
    primary.append_message.assert_called_once_with(
        "919999999999",
        "inbound",
        "Offline hello",
        "text",
        "wamid.inbound",
        client_id=7,
    )
    secondary.append_message.assert_called_once_with(
        "919999999999",
        "inbound",
        "Offline hello",
        "text",
        "wamid.inbound",
        client_id=7,
    )
    primary.update_lead_status.assert_called_once_with(
        "919999999999",
        "Contacted",
        client_id=7,
    )


def test_status_worker_resolves_and_forwards_tenant_context(monkeypatch):
    store, _, secondary = _dual_store()
    context = SimpleNamespace(client=SimpleNamespace(id=7))
    monkeypatch.setattr(jobs, "get_store", lambda: store)
    monkeypatch.setattr(
        jobs.tenant,
        "resolve_context_by_phone_id",
        lambda phone_number_id: context,
    )

    jobs.process_status_update(
        {"id": "wamid.outbound", "status": "delivered"},
        phone_number_id="offline-phone-number-id",
    )

    secondary.update_message_status.assert_called_once_with(
        "wamid.outbound",
        "delivered",
        client_id=7,
    )


def test_lead_phone_constraint_is_tenant_scoped():
    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in Lead.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert Lead.__table__.c.phone.unique is not True
    assert unique_constraints["uq_leads_client_phone"] == ("client_id", "phone")


def test_same_phone_across_tenants_allowed_but_same_tenant_duplicate_rejected():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Client.__table__, Lead.__table__],
    )

    with Session(engine) as session:
        session.add_all(
            [
                Client(id=1, name="Tenant A"),
                Client(id=2, name="Tenant B"),
            ]
        )
        session.add_all(
            [
                Lead(
                    phone="919999999999",
                    name="Tenant A Lead",
                    client_id=1,
                ),
                Lead(
                    phone="919999999999",
                    name="Tenant B Lead",
                    client_id=2,
                ),
            ]
        )
        session.commit()

        session.add(
            Lead(
                phone="919999999999",
                name="Tenant A Duplicate",
                client_id=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        rows = session.execute(
            select(Lead).where(Lead.phone == "919999999999")
        ).scalars().all()
        assert {(lead.client_id, lead.phone) for lead in rows} == {
            (1, "919999999999"),
            (2, "919999999999"),
        }

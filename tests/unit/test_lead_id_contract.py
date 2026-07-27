import inspect
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.routers import leads


def _request(path: str = "/api/leads") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": ("offline-test", 1),
            "server": ("offline-test", 80),
            "scheme": "http",
        }
    )


def _route(function):
    return inspect.unwrap(function)


def _record(record_id: str, *, phone: str = "919999999999", status: str = "New Lead"):
    return {
        "id": record_id,
        "fields": {
            "Name": "Offline Lead",
            "Phone number type": phone,
            "Status": status,
            "Last_Message": "",
            "Lead_Score": "Warm",
        },
    }


class FakeDualStore:
    def __init__(self):
        self.record = _record("recOfflineLead")
        self.updated_ids = []
        self.messages = []

    def get_all_leads(self, client_id=None):
        return [self.record]

    def _search(self, formula, client_id=None):
        return [self.record]

    def get_lead_by_id(self, record_id, client_id=None):
        return self.record if str(record_id) == self.record["id"] else None

    def get_lead(self, phone, client_id=None):
        return self.record if phone == self.record["fields"]["Phone number type"] else None

    def update_lead_status_by_id(self, record_id, status, client_id=None):
        if record_id != self.record["id"]:
            return None
        self.record["fields"]["Status"] = status
        self.updated_ids.append(record_id)
        return self.record

    def append_message(self, **message):
        self.messages.append(message)
        return True


class FakePostgresStore:
    def __init__(self):
        self.records = {
            (7, 1): _record("7"),
            (8, 2): _record("8", phone="918888888888"),
        }
        self.calls = []

    def get_lead_by_id(self, lead_id, client_id):
        self.calls.append((lead_id, client_id))
        return self.records.get((int(lead_id), client_id))

    def get_lead(self, *args, **kwargs):
        raise AssertionError("Postgres detail must resolve by scoped ID, not phone")


def test_dual_mode_list_detail_stage_uses_same_airtable_id(monkeypatch):
    fake_store = FakeDualStore()
    client = SimpleNamespace(id=1)
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: False)

    listed = _route(leads.list_leads)(_request(), Response(), client, None)
    returned_id = listed[0]["id"]

    assert returned_id == "recOfflineLead"
    assert isinstance(returned_id, str)

    detail = _route(leads.get_lead_detail)(
        _request(f"/api/leads/{returned_id}"),
        Response(),
        returned_id,
        client,
    )
    assert detail["id"] == returned_id

    updated = _route(leads.update_lead_stage)(
        _request(f"/api/leads/{returned_id}/stage"),
        Response(),
        returned_id,
        leads.StageUpdateBody(stage="Contacted"),
        client,
    )
    assert updated == {"success": True, "stage": "Contacted"}
    assert fake_store.updated_ids == [returned_id]


def test_postgres_detail_returns_scoped_lead_as_string_id(monkeypatch):
    fake_store = FakePostgresStore()
    client = SimpleNamespace(id=1)
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: True)

    detail = _route(leads.get_lead_detail)(
        _request("/api/leads/7"),
        Response(),
        "7",
        client,
    )

    assert detail["id"] == "7"
    assert fake_store.calls == [(7, 1)]


def test_postgres_detail_missing_lead_returns_404(monkeypatch):
    monkeypatch.setattr(leads, "store", FakePostgresStore())
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: True)

    with pytest.raises(HTTPException) as error:
        _route(leads.get_lead_detail)(
            _request("/api/leads/999"),
            Response(),
            "999",
            SimpleNamespace(id=1),
        )

    assert error.value.status_code == 404


def test_postgres_detail_blocks_cross_tenant_access(monkeypatch):
    fake_store = FakePostgresStore()
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: True)

    with pytest.raises(HTTPException) as error:
        _route(leads.get_lead_detail)(
            _request("/api/leads/8"),
            Response(),
            "8",
            SimpleNamespace(id=1),
        )

    assert error.value.status_code == 404
    assert fake_store.calls == [(8, 1)]


def test_dual_mode_related_routes_accept_returned_airtable_id(monkeypatch):
    fake_store = FakeDualStore()
    client = SimpleNamespace(id=1)
    db_lead = SimpleNamespace(id=42, is_human_takeover=False)
    db_message = SimpleNamespace(
        id=5,
        direction="OUTBOUND",
        msg_type="human",
        body="Manual reply",
        created_at=datetime(2026, 1, 1, 10, 0),
        status="sent",
        channel="whatsapp",
        subject=None,
    )

    lead_query = MagicMock()
    lead_query.filter.return_value = lead_query
    lead_query.first.return_value = db_lead
    message_query = MagicMock()
    message_query.filter.return_value = message_query
    message_query.order_by.return_value = message_query
    message_query.all.return_value = [db_message]

    session = MagicMock()
    session.query.side_effect = (
        lambda model: lead_query if model is leads.Lead else message_query
    )

    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: False)
    monkeypatch.setattr(
        leads,
        "_postgres_lead_id_for_record",
        lambda lead_id, record, client_id: 42,
    )
    monkeypatch.setattr(leads, "SessionLocal", lambda: nullcontext(session))
    send = MagicMock(return_value="wamid.offline")
    monkeypatch.setattr(leads.whatsapp, "send_message", send)

    public_id = "recOfflineLead"
    messages = _route(leads.get_lead_messages)(
        _request(f"/api/leads/{public_id}/messages"),
        Response(),
        public_id,
        client,
    )
    assert messages[0]["role"] == "human"

    takeover = _route(leads.takeover_lead)(
        _request(f"/api/leads/{public_id}/takeover"),
        Response(),
        public_id,
        client,
    )
    assert takeover["lead_id"] == public_id
    assert db_lead.is_human_takeover is True

    release = _route(leads.release_lead)(
        _request(f"/api/leads/{public_id}/release"),
        Response(),
        public_id,
        client,
    )
    assert release["lead_id"] == public_id
    assert db_lead.is_human_takeover is False

    sent = _route(leads.send_human_message)(
        _request(f"/api/leads/{public_id}/send-message"),
        Response(),
        public_id,
        leads.SendMessageBody(message="Manual reply"),
        client,
    )
    assert sent == {"success": True}
    send.assert_called_once_with("919999999999", "Manual reply")
    assert fake_store.messages[0]["msg_type"] == "human"

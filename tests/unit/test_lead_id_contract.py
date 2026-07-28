import inspect
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.api.routers import leads
from app.store.store import DualWriteStore


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
            "is_human_takeover": False,
        },
    }


class FakeDualStore:
    def __init__(self):
        self.record = _record("recOfflineLead")
        self.updated_ids = []
        self.messages = []

    def get_all_leads(self, client_id):
        return [self.record]

    def _search(self, formula, client_id):
        return [self.record]

    def get_lead_by_id(self, record_id, client_id):
        return self.record if str(record_id) == self.record["id"] else None

    def get_lead(self, phone, client_id):
        return self.record if phone == self.record["fields"]["Phone number type"] else None

    def update_lead_status_by_id(self, record_id, status, client_id):
        if record_id != self.record["id"]:
            return None
        self.record["fields"]["Status"] = status
        self.updated_ids.append(record_id)
        return self.record

    def update_human_takeover_by_id(self, record_id, enabled, client_id):
        if record_id != self.record["id"]:
            return None
        self.record["fields"]["is_human_takeover"] = enabled
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
        self.updated_ids = []
        self.messages = []

    def get_lead_by_id(self, lead_id, client_id):
        self.calls.append((lead_id, client_id))
        return self.records.get((int(lead_id), client_id))

    def get_all_leads(self, client_id):
        return [
            record
            for (lead_id, owner_id), record in self.records.items()
            if owner_id == client_id
        ]

    def _search(self, formula, client_id):
        return self.get_all_leads(client_id)

    def get_lead(self, *args, **kwargs):
        raise AssertionError("Postgres detail must resolve by scoped ID, not phone")

    def update_lead_status_by_id(self, record_id, status, client_id):
        record = self.records.get((int(record_id), client_id))
        if not record:
            return None
        record["fields"]["Status"] = status
        self.updated_ids.append((str(record_id), client_id))
        return record

    def update_lead_status(self, phone, status, client_id):
        for (lead_id, owner_id), record in self.records.items():
            if (
                owner_id == client_id
                and record["fields"]["Phone number type"] == phone
            ):
                record["fields"]["Status"] = status
                self.updated_ids.append((str(lead_id), client_id))
                return record
        return None

    def update_human_takeover_by_id(self, record_id, enabled, client_id):
        record = self.records.get((int(record_id), client_id))
        if not record:
            return None
        record["fields"]["is_human_takeover"] = enabled
        return record

    def append_message(self, **message):
        self.messages.append(message)
        return True


@pytest.mark.parametrize("mode", ["airtable", "dual"])
def test_airtable_backed_modes_list_detail_stage_use_same_id(monkeypatch, mode):
    fake_store = FakeDualStore()
    active_store = (
        fake_store
        if mode == "airtable"
        else DualWriteStore(cast(Any, fake_store), FakePostgresStore())
    )
    client = SimpleNamespace(id=1)
    monkeypatch.setattr(leads, "store", active_store)
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


def test_airtable_only_messages_takeover_and_release_use_list_id(monkeypatch):
    fake_store = FakeDualStore()
    fake_store.record["fields"]["Last_Message"] = (
        "[2026-01-01 10:00:00] INBOUND (text): Offline hello"
    )
    client = SimpleNamespace(id=1)
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: False)
    monkeypatch.setattr(
        leads,
        "_postgres_lead_id_for_record",
        lambda lead_id, record, client_id: None,
    )
    monkeypatch.setattr(leads, "SessionLocal", None)
    send = MagicMock(return_value="wamid.airtable-offline")
    monkeypatch.setattr(leads.whatsapp, "send_message", send)

    public_id = _route(leads.list_leads)(_request(), Response(), client, None)[0]["id"]
    messages = _route(leads.get_lead_messages)(
        _request(f"/api/leads/{public_id}/messages"),
        Response(),
        public_id,
        client,
    )
    assert messages == [
        {
            "id": "m0",
            "role": "user",
            "content": "Offline hello",
            "timestamp": "10:00 AM",
        }
    ]

    takeover = _route(leads.takeover_lead)(
        _request(f"/api/leads/{public_id}/takeover"),
        Response(),
        public_id,
        client,
    )
    assert takeover == {
        "success": True,
        "lead_id": public_id,
        "is_human_takeover": True,
    }
    assert fake_store.record["fields"]["is_human_takeover"] is True

    release = _route(leads.release_lead)(
        _request(f"/api/leads/{public_id}/release"),
        Response(),
        public_id,
        client,
    )
    assert release == {
        "success": True,
        "lead_id": public_id,
        "is_human_takeover": False,
    }
    assert fake_store.record["fields"]["is_human_takeover"] is False

    sent = _route(leads.send_human_message)(
        _request(f"/api/leads/{public_id}/send-message"),
        Response(),
        public_id,
        leads.SendMessageBody(message="Offline human reply"),
        client,
    )
    assert sent == {"success": True}
    send.assert_called_once_with("919999999999", "Offline human reply")
    assert fake_store.messages[-1]["client_id"] == 1


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


def test_postgres_takeover_blocks_cross_tenant_access(monkeypatch):
    fake_store = FakePostgresStore()
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: True)

    with pytest.raises(HTTPException) as error:
        _route(leads.takeover_lead)(
            _request("/api/leads/8/takeover"),
            Response(),
            "8",
            SimpleNamespace(id=1),
        )

    assert error.value.status_code == 404
    assert fake_store.records[(8, 2)]["fields"]["is_human_takeover"] is False


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
    assert fake_store.record["fields"]["is_human_takeover"] is True

    release = _route(leads.release_lead)(
        _request(f"/api/leads/{public_id}/release"),
        Response(),
        public_id,
        client,
    )
    assert release["lead_id"] == public_id
    assert fake_store.record["fields"]["is_human_takeover"] is False

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


def test_postgres_mode_all_related_routes_accept_list_id(monkeypatch):
    fake_store = FakePostgresStore()
    client = SimpleNamespace(id=1)
    db_lead = SimpleNamespace(id=7, is_human_takeover=False)
    db_message = SimpleNamespace(
        id=9,
        direction="INBOUND",
        msg_type="text",
        body="Hello",
        created_at=datetime(2026, 1, 1, 10, 0),
        status="delivered",
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
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: True)
    monkeypatch.setattr(leads, "SessionLocal", lambda: nullcontext(session))
    send = MagicMock(return_value="wamid.offline")
    monkeypatch.setattr(leads.whatsapp, "send_message", send)

    listed = _route(leads.list_leads)(_request(), Response(), client, None)
    public_id = listed[0]["id"]
    assert public_id == "7"

    detail = _route(leads.get_lead_detail)(
        _request(f"/api/leads/{public_id}"),
        Response(),
        public_id,
        client,
    )
    assert detail["id"] == public_id

    messages = _route(leads.get_lead_messages)(
        _request(f"/api/leads/{public_id}/messages"),
        Response(),
        public_id,
        client,
    )
    assert messages[0]["role"] == "user"

    stage = _route(leads.update_lead_stage)(
        _request(f"/api/leads/{public_id}/stage"),
        Response(),
        public_id,
        leads.StageUpdateBody(stage="Contacted"),
        client,
    )
    assert stage == {"success": True, "stage": "Contacted"}
    assert fake_store.updated_ids == [("7", 1)]

    takeover = _route(leads.takeover_lead)(
        _request(f"/api/leads/{public_id}/takeover"),
        Response(),
        public_id,
        client,
    )
    assert takeover["lead_id"] == public_id
    assert fake_store.records[(7, 1)]["fields"]["is_human_takeover"] is True

    release = _route(leads.release_lead)(
        _request(f"/api/leads/{public_id}/release"),
        Response(),
        public_id,
        client,
    )
    assert release["lead_id"] == public_id
    assert fake_store.records[(7, 1)]["fields"]["is_human_takeover"] is False

    sent = _route(leads.send_human_message)(
        _request(f"/api/leads/{public_id}/send-message"),
        Response(),
        public_id,
        leads.SendMessageBody(message="Manual reply"),
        client,
    )
    assert sent == {"success": True}
    assert fake_store.messages == [
        {
            "phone": "919999999999",
            "client_id": 1,
            "direction": "OUTBOUND",
            "message": "Manual reply",
            "msg_type": "human",
        }
    ]


def test_postgres_mode_rejects_airtable_id(monkeypatch):
    monkeypatch.setattr(leads, "store", FakePostgresStore())
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: True)

    with pytest.raises(HTTPException) as error:
        _route(leads.get_lead_detail)(
            _request("/api/leads/recStale"),
            Response(),
            "recStale",
            SimpleNamespace(id=1),
        )

    assert error.value.status_code == 404


def test_dual_legacy_numeric_id_is_scoped_and_logged(monkeypatch, caplog):
    fake_store = FakeDualStore()
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: False)
    monkeypatch.setattr(leads, "LEGACY_LEAD_ID_COMPAT_ENABLED", True)
    monkeypatch.setattr(
        leads,
        "_load_postgres_lead",
        lambda *, lead_id, phone=None, client_id: (
            SimpleNamespace(id=lead_id, phone="919999999999")
            if (lead_id, client_id) == (42, 1)
            else None
        ),
    )

    with caplog.at_level("WARNING"):
        record = leads._store_record_for_lead_id("42", client_id=1)

    assert record is not None
    assert record["id"] == "recOfflineLead"
    assert any(
        getattr(log_record, "event", None) == "legacy_lead_id_resolved"
        and getattr(log_record, "client_id", None) == 1
        for log_record in caplog.records
    )
    assert leads._store_record_for_lead_id("42", client_id=2) is None


def test_dual_stale_legacy_id_and_disabled_compatibility_fail_closed(monkeypatch):
    fake_store = FakeDualStore()
    get_lead = MagicMock(wraps=fake_store.get_lead)
    monkeypatch.setattr(fake_store, "get_lead", get_lead)
    monkeypatch.setattr(leads, "store", fake_store)
    monkeypatch.setattr(leads, "_is_postgres_store", lambda: False)
    monkeypatch.setattr(
        leads,
        "_load_postgres_lead",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(leads, "LEGACY_LEAD_ID_COMPAT_ENABLED", True)
    assert leads._store_record_for_lead_id("999", client_id=1) is None

    monkeypatch.setattr(leads, "LEGACY_LEAD_ID_COMPAT_ENABLED", False)
    assert leads._store_record_for_lead_id("42", client_id=1) is None
    get_lead.assert_not_called()

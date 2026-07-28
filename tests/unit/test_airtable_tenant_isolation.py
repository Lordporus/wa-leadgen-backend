from types import SimpleNamespace
from unittest.mock import MagicMock

from app.clients.airtable_client import AirtableClient
from app.store.store import DualWriteStore


def _configured_airtable(client_id: int = 1) -> AirtableClient:
    client = AirtableClient()
    client.client_id = client_id
    client.ok = True
    client.base_url = "https://offline.invalid/leads"
    client.headers = {}
    return client


def test_airtable_record_id_lookup_rejects_cross_tenant_before_network(monkeypatch):
    record = {"id": "recTenantA", "fields": {"Name": "Tenant A lead"}}
    get = MagicMock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: record,
        )
    )
    monkeypatch.setattr("app.clients.airtable_client.requests.get", get)
    store = DualWriteStore(_configured_airtable(client_id=1), MagicMock())

    assert store.get_lead_by_id("recTenantA", client_id=1) == record
    assert store.get_lead_by_id("recTenantA", client_id=2) is None
    get.assert_called_once()


def test_airtable_cached_phone_lookup_is_scoped_by_configured_tenant(monkeypatch):
    record = {
        "id": "recCachedTenantA",
        "fields": {"Phone number type": "919999999999"},
    }
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"records": [record]},
    )
    get = MagicMock(return_value=response)
    monkeypatch.setattr("app.clients.airtable_client.requests.get", get)
    store = DualWriteStore(_configured_airtable(client_id=1), MagicMock())

    assert store.get_lead("919999999999", client_id=1) == record
    assert store.get_lead("919999999999", client_id=2) is None
    get.assert_called_once()


def test_airtable_writes_reject_cross_tenant_before_network(monkeypatch):
    client = _configured_airtable(client_id=1)
    get = MagicMock()
    post = MagicMock()
    patch = MagicMock()
    monkeypatch.setattr("app.clients.airtable_client.requests.get", get)
    monkeypatch.setattr("app.clients.airtable_client.requests.post", post)
    monkeypatch.setattr("app.clients.airtable_client.requests.patch", patch)

    assert client.add_lead("Tenant B", "918888888888", client_id=2) is None
    assert client.update_lead_status(
        "918888888888",
        "Contacted",
        client_id=2,
    ) is None
    assert client.update_human_takeover_by_id(
        "recTenantB",
        True,
        client_id=2,
    ) is None
    assert client.append_message(
        "918888888888",
        "inbound",
        "Offline message",
        client_id=2,
    ) is False
    client.update_message_status("wamid.offline", "delivered", client_id=2)
    client.update_lead_info(
        "918888888888",
        "Tenant B",
        None,
        client_id=2,
    )
    client.update_lead_score("918888888888", "Warm", client_id=2)

    get.assert_not_called()
    post.assert_not_called()
    patch.assert_not_called()


def test_airtable_takeover_updates_only_owned_record(monkeypatch):
    client = _configured_airtable(client_id=1)
    record = {
        "id": "recTenantA",
        "fields": {
            "Phone number type": "919999999999",
            "is_human_takeover": False,
        },
    }
    updated = {
        **record,
        "fields": {
            **record["fields"],
            "is_human_takeover": True,
        },
    }
    get = MagicMock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: record,
        )
    )
    patch = MagicMock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: updated,
        )
    )
    monkeypatch.setattr("app.clients.airtable_client.requests.get", get)
    monkeypatch.setattr("app.clients.airtable_client.requests.patch", patch)

    assert client.update_human_takeover_by_id(
        "recTenantA",
        True,
        client_id=1,
    ) == updated
    patch.assert_called_once_with(
        "https://offline.invalid/leads/recTenantA",
        headers={},
        json={"fields": {"is_human_takeover": True}, "typecast": True},
        timeout=10,
    )

    assert client.update_human_takeover_by_id(
        "recTenantA",
        False,
        client_id=2,
    ) is None
    assert get.call_count == 1
    assert patch.call_count == 1

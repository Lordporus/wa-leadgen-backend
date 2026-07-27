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

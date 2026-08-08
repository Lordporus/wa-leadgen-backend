import inspect
from types import SimpleNamespace
from app.api.routers import leads


def test_dashboard_stats_uses_store_backed_leads_and_tenant_won_lost(monkeypatch):
    """
    Test 1, 2, 3, 4, 7: Ensure get_dashboard_stats reads from store.get_all_leads
    and uses tenant won/lost stage definitions.
    """
    mock_store_leads = [
        {"id": "1", "fields": {"Status": "New Lead", "Created_At": "2026-08-01 10:00:00"}},
        {"id": "2", "fields": {"Status": "Contacted", "Created_At": "2026-08-02 10:00:00"}},
        {"id": "3", "fields": {"Status": "Qualified", "Created_At": "2026-08-03 10:00:00"}},
        {"id": "4", "fields": {"Status": "Booked", "Created_At": "2026-08-04 10:00:00"}},
        {"id": "5", "fields": {"Status": "Booked", "Created_At": "2026-08-05 10:00:00"}},
        {"id": "6", "fields": {"Status": "Booked", "Created_At": "2026-08-06 10:00:00"}},
        {"id": "7", "fields": {"Status": "Lost", "Created_At": "2026-08-07 10:00:00"}},
    ]

    called_client_ids = []

    def mock_get_all_leads(client_id):
        called_client_ids.append(client_id)
        return mock_store_leads

    def mock_get_won_stage_names(client_id):
        return ["Booked"]

    def mock_get_lost_stage_names(client_id):
        return ["Lost"]

    monkeypatch.setattr(leads.store, "get_all_leads", mock_get_all_leads)
    monkeypatch.setattr(leads.tenant, "get_won_stage_names", mock_get_won_stage_names)
    monkeypatch.setattr(leads.tenant, "get_lost_stage_names", mock_get_lost_stage_names)

    mock_client = SimpleNamespace(id=42)

    res = inspect.unwrap(leads.get_dashboard_stats)(None, None, client=mock_client)

    assert called_client_ids == [42]
    assert res["total"] == 7
    assert res["booked"] == 3
    assert res["lost"] == 1
    # 3 / 7 * 100 = 42.857... -> rounded to 43
    assert res["conversion_rate"] == 43


def test_dashboard_stats_zero_leads_returns_zero_conversion(monkeypatch):
    """
    Test 5: Zero leads returns conversion_rate = 0 safely.
    """
    monkeypatch.setattr(leads.store, "get_all_leads", lambda client_id: [])
    monkeypatch.setattr(leads.tenant, "get_won_stage_names", lambda client_id: ["Booked"])
    monkeypatch.setattr(leads.tenant, "get_lost_stage_names", lambda client_id: ["Lost"])

    mock_client = SimpleNamespace(id=99)
    res = inspect.unwrap(leads.get_dashboard_stats)(None, None, client=mock_client)

    assert res["total"] == 0
    assert res["booked"] == 0
    assert res["lost"] == 0
    assert res["conversion_rate"] == 0


def test_dashboard_stats_tenant_isolation(monkeypatch):
    """
    Test 6: Ensure tenant client_id is isolated and passed accurately to store.
    """
    requested_ids = []

    def mock_get_all_leads(client_id):
        requested_ids.append(("store", client_id))
        return []

    def mock_get_won_stage_names(client_id):
        requested_ids.append(("won", client_id))
        return ["Booked"]

    def mock_get_lost_stage_names(client_id):
        requested_ids.append(("lost", client_id))
        return ["Lost"]

    monkeypatch.setattr(leads.store, "get_all_leads", mock_get_all_leads)
    monkeypatch.setattr(leads.tenant, "get_won_stage_names", mock_get_won_stage_names)
    monkeypatch.setattr(leads.tenant, "get_lost_stage_names", mock_get_lost_stage_names)

    mock_client = SimpleNamespace(id=777)
    inspect.unwrap(leads.get_dashboard_stats)(None, None, client=mock_client)

    assert requested_ids == [("store", 777), ("won", 777), ("lost", 777)]

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import dependencies
from app.api.routers import leads
from app.core import database
from app.services import tenant


def _active_client():
    return SimpleNamespace(
        id=1,
        name="Offline Tenant",
        is_active=True,
        system_prompt="",
        calendly_link="",
        pipeline_stages=[],
        leads=[],
    )


def _configured_session(monkeypatch, client):
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = client
    monkeypatch.setattr(database, "engine", object())
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(session))
    return session


def test_app_startup_initialization_is_visible_to_tenant_auth(monkeypatch):
    client = _active_client()
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = client
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)
    monkeypatch.setattr(database, "create_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        database,
        "sessionmaker",
        lambda **kwargs: lambda: nullcontext(session),
    )
    monkeypatch.setattr(tenant, "get_gemini_for_client", lambda _: object())
    monkeypatch.setattr(tenant, "get_won_stage_names", lambda _: ["Booked"])
    monkeypatch.setattr(tenant, "get_lost_stage_names", lambda _: ["Lost"])

    # Simulate app/store startup after tenant.py has already been imported.
    database.init_engine("postgresql://offline-test")

    context = tenant.resolve_context_by_api_key("valid-offline-dashboard-key")

    assert context.client is client
    session.execute.assert_called_once()


def test_invalid_dashboard_api_key_remains_fail_closed(monkeypatch):
    _configured_session(monkeypatch, None)

    with pytest.raises(HTTPException) as error:
        dependencies.require_api_key(api_key="invalid-offline-dashboard-key")

    assert error.value.status_code == 403
    assert error.value.detail == "Invalid or missing API key"


def test_authenticated_leads_route_returns_data_without_provider_access(monkeypatch):
    from main import app

    client = _active_client()
    monkeypatch.setattr(
        tenant,
        "resolve_context_by_api_key",
        lambda raw_key: SimpleNamespace(client=client) if raw_key == "valid-offline-dashboard-key" else None,
    )
    monkeypatch.setattr(
        leads,
        "store",
        SimpleNamespace(
            get_all_leads=lambda client_id: [
                {
                    "id": "recOfflineLead",
                    "fields": {
                        "Name": "Offline Lead",
                        "Phone number type": "919999999999",
                        "Status": "New Lead",
                        "Last_Message": "",
                        "Lead_Score": "Warm",
                    },
                }
            ]
        ),
    )

    async def request_leads():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://offline.test",
        ) as test_client:
            return await test_client.get(
                "/api/leads",
                headers={"X-API-Key": "valid-offline-dashboard-key"},
            )

    response = asyncio.run(request_leads())

    assert response.status_code == 200
    assert response.json()[0]["id"] == "recOfflineLead"

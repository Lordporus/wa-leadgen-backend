import asyncio
import inspect
from contextlib import nullcontext
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routers import health
from scripts import run_migrations


def test_migration_actor_requires_explicit_release_gates():
    with pytest.raises(RuntimeError, match="expected current"):
        run_migrations.run_migrations(
            "postgresql://offline/never", backup_verified=True, approval_id="APR-1"
        )
    with pytest.raises(RuntimeError, match="backup"):
        run_migrations.run_migrations(
            "postgresql://offline/never",
            expected_current_revision="0021",
            expected_target_revision="0021",
            approval_id="APR-1",
        )
    with pytest.raises(RuntimeError, match="approval"):
        run_migrations.run_migrations(
            "postgresql://offline/never",
            expected_current_revision="0021",
            expected_target_revision="0021",
            backup_verified=True,
        )


def test_migration_actor_rejects_an_unexpected_database_revision(monkeypatch):
    connection = MagicMock()
    connection.execute.return_value.scalar_one_or_none.return_value = "0020"
    engine = MagicMock()
    engine.begin.return_value = nullcontext(connection)
    monkeypatch.setattr(run_migrations, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(run_migrations, "_configured_head", lambda _: "0021")

    with pytest.raises(RuntimeError, match="expected current revision"):
        run_migrations.run_migrations(
            "postgresql://offline/never",
            expected_current_revision="0021",
            expected_target_revision="0021",
            backup_verified=True,
            approval_id="APR-1",
        )


def test_migration_actor_rejects_an_unexpected_target_revision(monkeypatch):
    connection = MagicMock()
    engine = MagicMock()
    engine.begin.return_value = nullcontext(connection)
    monkeypatch.setattr(run_migrations, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(run_migrations, "_configured_head", lambda _: "0022")
    upgrade = MagicMock()
    monkeypatch.setattr(run_migrations.command, "upgrade", upgrade)

    with pytest.raises(RuntimeError, match="expected target revision"):
        run_migrations.run_migrations(
            "postgresql://offline/never",
            expected_current_revision="0021",
            expected_target_revision="0021",
            backup_verified=True,
            approval_id="APR-1",
        )

    upgrade.assert_not_called()


def test_readiness_is_secret_free_and_requires_required_configuration(monkeypatch):
    monkeypatch.setattr(health, "_health_payload", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "DATABASE_URL", "postgresql://configured")
    monkeypatch.setattr(health, "WHATSAPP_ACCESS_TOKEN", "not-returned")
    monkeypatch.setattr(health, "WHATSAPP_PHONE_NUMBER_ID", "not-returned")
    monkeypatch.setattr(health, "WHATSAPP_APP_SECRET", "not-returned")
    monkeypatch.setattr(health, "WHATSAPP_RQ_CONSUMER_ENABLED", True)
    monkeypatch.setattr(health, "WHATSAPP_OUTBOUND_ENABLED", False)
    monkeypatch.setattr(
        health,
        "_schema_readiness",
        lambda: {"ready": True, "required_revision": "0021", "current_revision": "0021"},
    )

    result = inspect.unwrap(health.readiness_check)(None, None)

    assert result["status"] == "ready"
    assert result["configuration"]["outbound_enabled"] is False
    assert "not-returned" not in str(result)


def test_readiness_fails_when_worker_consumption_is_disabled(monkeypatch):
    monkeypatch.setattr(health, "_health_payload", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "DATABASE_URL", "configured")
    monkeypatch.setattr(health, "WHATSAPP_ACCESS_TOKEN", "configured")
    monkeypatch.setattr(health, "WHATSAPP_PHONE_NUMBER_ID", "configured")
    monkeypatch.setattr(health, "WHATSAPP_APP_SECRET", "configured")
    monkeypatch.setattr(health, "WHATSAPP_RQ_CONSUMER_ENABLED", False)
    monkeypatch.setattr(
        health,
        "_schema_readiness",
        lambda: {"ready": True, "required_revision": "0021", "current_revision": "0021"},
    )

    with pytest.raises(HTTPException) as exc_info:
        inspect.unwrap(health.readiness_check)(None, None)

    assert exc_info.value.status_code == 503


def test_readiness_fails_for_a_schema_revision_mismatch(monkeypatch):
    monkeypatch.setattr(health, "_health_payload", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "DATABASE_URL", "configured")
    monkeypatch.setattr(health, "WHATSAPP_ACCESS_TOKEN", "configured")
    monkeypatch.setattr(health, "WHATSAPP_PHONE_NUMBER_ID", "configured")
    monkeypatch.setattr(health, "WHATSAPP_APP_SECRET", "configured")
    monkeypatch.setattr(health, "WHATSAPP_RQ_CONSUMER_ENABLED", True)
    monkeypatch.setattr(
        health,
        "_schema_readiness",
        lambda: {"ready": False, "required_revision": "0021", "current_revision": "0020"},
    )

    with pytest.raises(HTTPException) as exc_info:
        inspect.unwrap(health.readiness_check)(None, None)

    assert exc_info.value.status_code == 503


def test_readiness_route_handles_slowapi_response_injection(monkeypatch):
    monkeypatch.setattr(
        health,
        "_health_payload",
        lambda: {
            "status": "ok",
            "db": True,
            "redis": True,
            "whatsapp_queue": {"ready": True, "depth": 0, "workers": 1},
        },
    )
    monkeypatch.setattr(health, "DATABASE_URL", "configured")
    monkeypatch.setattr(health, "WHATSAPP_ACCESS_TOKEN", "configured")
    monkeypatch.setattr(health, "WHATSAPP_PHONE_NUMBER_ID", "configured")
    monkeypatch.setattr(health, "WHATSAPP_APP_SECRET", "configured")
    monkeypatch.setattr(health, "WHATSAPP_RQ_CONSUMER_ENABLED", True)
    monkeypatch.setattr(
        health,
        "_schema_readiness",
        lambda: {"ready": True, "required_revision": "0021", "current_revision": "0021"},
    )
    app = FastAPI()
    app.state.limiter = health.limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(health.router)

    async def request_readiness():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    response = asyncio.run(request_readiness())

    assert response.status_code == 200
    assert response.json()["status"] == "ready"

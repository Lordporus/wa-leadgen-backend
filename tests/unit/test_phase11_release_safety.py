import asyncio
import inspect
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routers import health
from scripts import run_migrations


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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


def test_migration_actor_fails_when_post_validation_does_not_reach_head(monkeypatch):
    connection = MagicMock()
    first_result = MagicMock()
    first_result.scalar_one_or_none.return_value = "0020"
    second_result = MagicMock()
    second_result.scalar_one_or_none.return_value = "0020"
    connection.execute.side_effect = [MagicMock(), first_result, second_result]
    engine = MagicMock()
    engine.begin.return_value = nullcontext(connection)
    monkeypatch.setattr(run_migrations, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(run_migrations, "_configured_head", lambda _: "0021")
    monkeypatch.setattr(run_migrations.command, "upgrade", MagicMock())

    with pytest.raises(RuntimeError, match="post-validation failed"):
        run_migrations.run_migrations(
            "postgresql://offline/never",
            expected_current_revision="0020",
            expected_target_revision="0021",
            backup_verified=True,
            approval_id="APR-2",
        )


def test_azure_release_workflow_pins_approved_ci_commit_without_topology_change():
    workflow = (REPOSITORY_ROOT / ".github/workflows/deploy-azure.yml").read_text()

    assert "DEPLOY_ROOT: /opt/qualify/backend" in workflow
    assert "environment: production" in workflow
    assert "workflow_dispatch:" in workflow
    assert "git merge-base --is-ancestor \"$TARGET_SHA\" origin/main" in workflow
    assert "Target SHA has no successful Backend CI push run" in workflow
    assert "ref: ${{ steps.target.outputs.sha }}" in workflow
    assert "StrictHostKeyChecking=yes" in workflow
    assert "Compose SHA-256" in workflow
    assert "/usr/local/sbin/qualify-deploy-azure backend" in workflow
    assert "scp " not in workflow
    assert "'$compose_digest'" in workflow


def test_migration_workflow_pins_main_commit_and_records_revision_manifest():
    workflow = (REPOSITORY_ROOT / ".github/workflows/release-migration.yml").read_text()

    assert "commit_sha:" in workflow
    assert "ref: ${{ inputs.commit_sha }}" in workflow
    assert "git merge-base --is-ancestor \"$TARGET_SHA\" origin/main" in workflow
    assert "Target SHA has no successful Backend CI push run" in workflow
    assert "expected_current_revision" in workflow
    assert "expected_target_revision" in workflow
    assert "backup_verified" in workflow
    assert "approval_id" in workflow
    assert "Record non-secret migration release manifest" in workflow


def test_azure_backend_rollout_orders_api_before_worker_and_never_migrates():
    script = (REPOSITORY_ROOT / "scripts/deploy-azure.sh").read_text()

    api_rollout = 'compose up -d --no-deps api'
    compatibility = "verify_api_compatibility"
    worker_rollout = 'compose up -d --no-deps worker'
    assert script.index(api_rollout) < script.rindex(compatibility) < script.index(worker_rollout)
    assert "ghcr.io/lordporus/wa-leadgen-backend:$commit_sha" in script
    assert "image reference does not match the component and commit SHA" in script
    assert "readonly DEPLOY_ROOT='/opt/qualify/backend'" in script
    assert "readonly BACKEND_ENV='/etc/qualify/backend.env'" in script
    assert "readonly FRONTEND_ENV='/etc/qualify/frontend.env'" in script
    assert "qualify-step5-compose.override.yml" not in script
    assert "rehearsal-backend.env" not in script
    assert "set_deployment_image \"$key\" \"$image\"" in script
    assert "readonly DEPLOY_LOCK='/var/lock/qualify-production-deploy.lock'" in script
    assert "live production Compose file does not match the reviewed backend commit" in script
    assert "write_deployment_images" in script
    assert "alembic upgrade" not in script
    assert "run_migrations.py" not in script


def test_phase11_runbook_requires_manifest_approvals_and_recovery_evidence():
    runbook = (REPOSITORY_ROOT / "docs/PHASE11_RELEASE_RUNBOOK.md").read_text()

    for required in (
        "SHA-256 of `deploy/docker-compose.production.yml`",
        "expected database revision",
        "staging or equivalent pre-production smoke evidence",
        "production deployment approval ID",
        "rollback owner",
        "post-release `/ready`",
        "worker rollback",
        "additive-schema application rollback",
    ):
        assert required in runbook
    assert "Render service/image IDs" not in runbook


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

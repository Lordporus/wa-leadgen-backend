from unittest.mock import MagicMock
from types import SimpleNamespace

from app.core import database
from app.core.models import Client
from app.services import jobs
from app.services import whatsapp_operations


def test_inbound_fallback_uses_session_factory_initialized_after_jobs_import(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(whatsapp_operations, "enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", None)
    assert not database.is_configured()

    database.init_engine(f"sqlite+pysqlite:///{(tmp_path / 'late-init.db').as_posix()}")
    assert database.engine is not None
    assert database.SessionLocal is not None
    database.Base.metadata.create_all(database.engine, tables=[Client.__table__])

    initialized_factory = database.SessionLocal
    opened_sessions = 0

    def tracking_session_factory():
        nonlocal opened_sessions
        opened_sessions += 1
        return initialized_factory()

    monkeypatch.setattr(database, "SessionLocal", tracking_session_factory)
    monkeypatch.setattr(jobs, "WHATSAPP_LOCAL_TEST_TENANT_FALLBACK", True)
    monkeypatch.setattr(jobs.tenant, "resolve_context_by_phone_id", lambda _phone_id: None)
    monkeypatch.setattr(jobs.tenant, "load_client", lambda _client_id: None)
    monkeypatch.setattr(jobs.tenant, "get_gemini_for_client", lambda _client: MagicMock())
    monkeypatch.setattr(jobs.tenant, "get_won_stage_names", lambda _client_id: [])
    monkeypatch.setattr(jobs.tenant, "get_lost_stage_names", lambda _client_id: [])
    monkeypatch.setattr(
        jobs.whatsapp_policy,
        "preflight_text",
        lambda **_kwargs: SimpleNamespace(allowed=True, reason_code="allowed"),
    )
    monkeypatch.setattr(
        jobs,
        "check_limit",
        lambda _client_id, _event_type, *, plan: (False, "offline limit"),
    )

    store = MagicMock()
    store.get_lead.return_value = {
        "id": "recLateInit",
        "fields": {
            "Status": "Contacted",
            "is_human_takeover": False,
        },
    }
    store.append_message.return_value = True
    monkeypatch.setattr(jobs, "get_store", lambda: store)

    try:
        jobs.process_webhook_message(
            "offline-phone-number-id",
            {
                "id": "wamid.late-init",
                "from": "919999999999",
                "type": "text",
                "text": {"body": "Offline hello"},
            },
        )
    finally:
        database.engine.dispose()

    assert opened_sessions == 1
    store.update_human_takeover_by_id.assert_called_once_with(
        "recLateInit",
        True,
        client_id=jobs.CLIENT_ID,
    )

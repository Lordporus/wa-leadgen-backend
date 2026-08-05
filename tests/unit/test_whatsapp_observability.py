import json
import logging
from types import SimpleNamespace

from fastapi.routing import APIRoute

from app.api.routers import whatsapp_observability as routes
from app.core.whatsapp_observability import ALERT_RULES, evaluate_alerts
from app.services import whatsapp_observability
from app.services.whatsapp_observability import (
    RedactingJsonFormatter,
    correlation_context,
    current_correlation_id,
    process_metrics,
)


def test_structured_log_fails_closed_for_unknown_nested_provider_fields():
    record = logging.LogRecord(
        "app.services.whatsapp_outbox",
        logging.ERROR,
        __file__,
        1,
        "send failed with raw customer body user@example.com",
        (),
        None,
    )
    record.event = "provider_send_failed"
    record.provider_error = {"unknown": {"text": "raw customer message"}}
    record.payload = {"body": "customer message", "token": "abc123"}
    record.correlation_id = "corr-safe"
    record.tenant_id = 42

    rendered = RedactingJsonFormatter().format(record)
    parsed = json.loads(rendered)

    assert parsed["event"] == "provider_send_failed"
    assert parsed["correlation_id"] == "corr-safe"
    assert parsed["tenant_id"] == 42
    assert parsed["payload"] == "[REDACTED]"
    assert parsed["provider_error"] == "[REDACTED]"
    for sensitive in ("user@example.com", "raw customer", "customer message", "abc123"):
        assert sensitive not in rendered


def test_correlation_context_requires_value_and_preserves_tenant():
    assert current_correlation_id() is None
    with correlation_context("corr-phase12b", tenant_id=12001):
        assert current_correlation_id() == "corr-phase12b"
        record = logging.LogRecord("app.services.jobs", logging.INFO, __file__, 1, "worker_event", (), None)
        parsed = json.loads(RedactingJsonFormatter().format(record))
        assert parsed["tenant_id"] == 12001
    assert current_correlation_id() is None


def test_process_metrics_labels_process_local_scope_and_tenant_duplicates():
    before = process_metrics.snapshot(client_id=12001)
    process_metrics.observe_webhook_ack(12.5, client_ids={12001})
    process_metrics.increment_duplicate(12001)

    tenant = process_metrics.snapshot(client_id=12001)
    other = process_metrics.snapshot(client_id=12002)
    assert tenant["webhook_ack_latency_ms"]["count"] == before["webhook_ack_latency_ms"]["count"] + 1
    assert tenant["duplicate_events_process_total"]["value"] == before["duplicate_events_process_total"]["value"] + 1
    assert tenant["duplicate_events_process_total"]["scope"] == "current_api_process"
    assert other["duplicate_events_process_total"]["value"] == 0


def test_collect_metrics_enforces_tenant_scope_bounds_controls_and_hides_infrastructure(monkeypatch):
    database_calls = []
    control_calls = []

    def fake_database_metrics(*, client_id, now):
        database_calls.append(client_id)
        return {"dead_letter_count": 2, "enqueue_failed_count": 3, "provider_status_failures_total": 4}

    def fake_states(*, client_id):
        control_calls.append(client_id)
        count = 205 if client_id == 42 else 1
        return [{"control": "tenant_outbound" if client_id else "global_outbound", "scope": "tenant" if client_id else "global", "resource_id": index if client_id else None, "effective_enabled": True, "version": 1} for index in range(count)]

    monkeypatch.setattr(whatsapp_observability, "_database_metrics", fake_database_metrics)
    monkeypatch.setattr(whatsapp_observability.whatsapp_operations, "list_states", fake_states)
    monkeypatch.setattr(whatsapp_observability, "_infrastructure_metrics", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("tenant infrastructure leak")))

    snapshot = whatsapp_observability.collect_metrics(client_id=42, include_infrastructure=False)
    assert database_calls == [42]
    assert control_calls == [42, None]
    assert snapshot["client_id"] == 42
    assert snapshot["metrics"]["dead_letter_count"] == 2
    assert snapshot["metrics"]["enqueue_failed_count"] == 3
    assert snapshot["metrics"]["provider_status_failures_total"] == 4
    assert len(snapshot["metrics"]["kill_switches"]) == 200
    assert snapshot["metrics"]["kill_switches_truncated"] is True
    assert "queue_depth" not in snapshot["metrics"]


def test_alert_evaluation_is_pure_and_does_not_consume_cooldown():
    metrics = {"worker_heartbeat_age_seconds": None, "oldest_queue_age_seconds": 301, "redis_available": False, "database_available": False, "provider_send_failures_15m": 5, "duplicate_send_invariant_breaches": 1, "dead_letter_count": 1, "kill_switch_active": True}
    first = evaluate_alerts(metrics)
    second = evaluate_alerts(metrics)

    assert first == second
    assert {alert.key for alert in first} == {rule.key for rule in ALERT_RULES}
    assert all(alert.evaluation_scope == "stateless" for alert in first)
    assert all(alert.cooldown_state == "not_tracked" for alert in first)


def test_observability_routes_are_protected_and_have_response_models():
    tenant_routes = [route for route in routes.tenant_router.routes if isinstance(route, APIRoute)]
    admin_routes = [route for route in routes.admin_router.routes if isinstance(route, APIRoute)]
    assert {route.path for route in tenant_routes} == {"/api/whatsapp-observability/metrics"}
    assert {route.path for route in admin_routes} == {"/api/admin/whatsapp-observability/metrics", "/api/admin/whatsapp-observability/alert-rules", "/api/admin/whatsapp-observability/alerts"}
    assert all(route.response_model is not None for route in tenant_routes + admin_routes)
    tenant_dependants = {dependency.call for route in tenant_routes for dependency in route.dependant.dependencies}
    admin_dependants = {dependency.call for route in admin_routes for dependency in route.dependant.dependencies}
    assert routes.require_api_key in tenant_dependants
    assert routes.require_admin in admin_dependants
    assert routes.require_admin_secret in admin_dependants


def test_tenant_endpoint_uses_only_authenticated_client(monkeypatch):
    calls = []

    def fake_collect_metrics(**kwargs):
        calls.append(kwargs)
        return {"metrics": {}}

    monkeypatch.setattr(whatsapp_observability, "collect_metrics", fake_collect_metrics)
    original = routes.tenant_metrics.__wrapped__
    original(SimpleNamespace(), SimpleNamespace(), client=SimpleNamespace(id=77))
    assert calls == [{"client_id": 77, "include_infrastructure": False}]

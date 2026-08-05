from datetime import datetime, timedelta, timezone

from app.services import whatsapp_alert_delivery


def _alert_metrics():
    return {
        "worker_heartbeat_age_seconds": 0,
        "oldest_queue_age_seconds": 0,
        "redis_available": True,
        "database_available": True,
        "provider_send_failures_15m": 5,
        "duplicate_send_invariant_breaches": 0,
        "dead_letter_count": 0,
        "kill_switch_active": False,
        "raw_payload": {"body": "must never leave"},
        "phone": "+91 98765 43210",
        "email": "customer@example.com",
        "token": "provider-secret-token",
    }


def test_alert_delivery_is_redacted_deduplicated_and_cooldown_aware(monkeypatch):
    whatsapp_alert_delivery._cooldowns.clear()
    monkeypatch.setattr(whatsapp_alert_delivery.whatsapp_observability, "collect_metrics", lambda **_kwargs: {"metrics": _alert_metrics()})
    captured: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)

    first = whatsapp_alert_delivery.deliver_active_alerts(capture=captured.append, now=now)
    second = whatsapp_alert_delivery.deliver_active_alerts(capture=captured.append, now=now + timedelta(seconds=1))
    third = whatsapp_alert_delivery.deliver_active_alerts(capture=captured.append, now=now + timedelta(seconds=901))

    assert first["delivered"] == 1
    assert second["suppressed"] == 1
    assert third["delivered"] == 1
    assert first["cooldown_scope"] == "current_api_process"
    assert captured[0]["stable_id"] == "meta_send_failure_spike"
    assert captured[0]["fingerprint"] == ["whatsapp-operational-alert", "global", "meta_send_failure_spike"]
    assert captured[0]["tenant_id"] is None
    assert "runbook" in captured[0]
    rendered = repr(captured)
    for secret in ("must never leave", "98765", "customer@example.com", "provider-secret-token"):
        assert secret not in rendered


def test_alert_delivery_failure_isolated_and_does_not_consume_cooldown(monkeypatch):
    whatsapp_alert_delivery._cooldowns.clear()
    monkeypatch.setattr(whatsapp_alert_delivery.whatsapp_observability, "collect_metrics", lambda **_kwargs: {"metrics": _alert_metrics()})
    now = datetime.now(timezone.utc)

    failed = whatsapp_alert_delivery.deliver_active_alerts(capture=lambda _payload: (_ for _ in ()).throw(RuntimeError("offline")), now=now)
    captured: list[dict[str, object]] = []
    recovered = whatsapp_alert_delivery.deliver_active_alerts(capture=captured.append, now=now)

    assert failed["failed"] == 1
    assert recovered["delivered"] == 1
    assert len(captured) == 1


def test_alert_delivery_is_noop_when_sentry_disabled(monkeypatch):
    whatsapp_alert_delivery._cooldowns.clear()
    monkeypatch.setattr(whatsapp_alert_delivery.config, "SENTRY_DSN", "")
    monkeypatch.setattr(whatsapp_alert_delivery.whatsapp_observability, "collect_metrics", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("disabled delivery must not evaluate")))

    result = whatsapp_alert_delivery.deliver_active_alerts()
    assert result["disabled"] is True
    assert result["attempted"] == 0

"""Best-effort delivery of Phase 12B alerts to the existing Sentry destination."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from app.core import config
from app.core.whatsapp_observability import ActiveAlert, evaluate_alerts
from app.core.whatsapp_phase12c import ALERT_RUNBOOKS
from app.services import whatsapp_observability

logger = logging.getLogger(__name__)
Capture = Callable[[dict[str, Any]], None]


class _Cooldowns:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._delivered: dict[str, datetime] = {}

    def claim(self, key: str, *, now: datetime, seconds: int) -> bool:
        with self._lock:
            prior = self._delivered.get(key)
            if prior is not None and (now - prior).total_seconds() < seconds:
                return False
            self._delivered[key] = now
            return True

    def release(self, key: str, *, claimed_at: datetime) -> None:
        with self._lock:
            if self._delivered.get(key) == claimed_at:
                self._delivered.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._delivered.clear()


_cooldowns = _Cooldowns()


def _safe_payload(alert: ActiveAlert) -> dict[str, Any]:
    return {
        "stable_id": alert.key,
        "fingerprint": ["whatsapp-operational-alert", "global", alert.key],
        "severity": alert.severity,
        "owner": alert.owner,
        "metric": alert.metric,
        "threshold": alert.threshold,
        "current_value": alert.current_value if isinstance(alert.current_value, (bool, int, float)) or alert.current_value is None else "unavailable",
        "runbook": ALERT_RUNBOOKS[alert.key],
        "scope": "global",
        "tenant_id": None,
    }


def _capture_sentry(payload: dict[str, Any]) -> None:
    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        scope.fingerprint = list(payload["fingerprint"])
        scope.set_tags({
            "whatsapp_alert_id": payload["stable_id"],
            "runbook": payload["runbook"],
            "severity": payload["severity"],
            "owner": payload["owner"],
            "scope": payload["scope"],
        })
        scope.set_context("whatsapp_operational_alert", {
            "metric": payload["metric"],
            "threshold": payload["threshold"],
            "current_value": payload["current_value"],
        })
        sentry_sdk.capture_message(
            f"WhatsApp operational alert: {payload['stable_id']}",
            level="fatal" if payload["severity"] == "critical" else "error",
        )


def deliver_active_alerts(
    *,
    capture: Capture | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deliver due alerts without ever interrupting WhatsApp processing."""
    when = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "destination": "sentry",
        "cooldown_scope": "current_api_process",
        "attempted": 0,
        "delivered": 0,
        "suppressed": 0,
        "failed": 0,
        "disabled": not bool(config.SENTRY_DSN),
    }
    if not config.SENTRY_DSN and capture is None:
        return result
    try:
        snapshot = whatsapp_observability.collect_metrics(client_id=None, include_infrastructure=True)
        alerts = evaluate_alerts(snapshot["metrics"])
    except Exception as exc:
        result["failed"] = 1
        logger.error("whatsapp_alert_evaluation_failed", extra={"error_type": type(exc).__name__})
        return result
    adapter = capture or _capture_sentry
    for alert in alerts:
        dedupe_key = f"global:{alert.key}"
        if not _cooldowns.claim(dedupe_key, now=when, seconds=alert.cooldown_seconds):
            result["suppressed"] += 1
            continue
        result["attempted"] += 1
        try:
            adapter(_safe_payload(alert))
        except Exception as exc:
            result["failed"] += 1
            logger.error("whatsapp_alert_delivery_failed", extra={"error_type": type(exc).__name__})
            _cooldowns.release(dedupe_key, claimed_at=when)
            continue
        result["delivered"] += 1
    return result

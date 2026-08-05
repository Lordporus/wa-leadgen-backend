"""Pure Phase 12B observability contracts, redaction, and alert evaluation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

MAX_CONTROL_STATES = 200
MAX_ALERTS = 16
REDACTED = "[REDACTED]"
_SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_SAFE_EXTRA_KEYS = frozenset({"client_id", "component", "control", "correlation_id", "count", "error_type", "event", "latency_ms", "provider_status", "reason_code", "resource_id", "scope", "state", "tenant_id", "version"})


def safe_event_name(value: object) -> str:
    candidate = str(value)
    return candidate if _SAFE_EVENT.fullmatch(candidate) else "unstructured_log_event"


def redact_log_value(value: Any, *, key: str | None = None) -> Any:
    """Allow only explicitly approved structured scalar fields; fail closed."""
    if key is None or key not in _SAFE_EXTRA_KEYS:
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if key in {"event", "component", "control", "provider_status", "reason_code", "scope", "state", "error_type"}:
        return safe_event_name(value)
    if key == "correlation_id":
        candidate = str(value).strip()
        return candidate if candidate and len(candidate) <= 128 else REDACTED
    return REDACTED


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AckLatencyMetric(StrictModel):
    count: int = Field(ge=0)
    average: float | None = Field(default=None, ge=0)
    maximum: float | None = Field(default=None, ge=0)
    latest: float | None = Field(default=None, ge=0)
    scope: Literal["current_api_process"]


class ProcessCounterMetric(StrictModel):
    value: int = Field(ge=0)
    scope: Literal["current_api_process"]


class KillSwitchMetric(StrictModel):
    control: str
    scope: str
    resource_id: int | None
    effective_enabled: bool
    version: int = Field(ge=1)


class MetricsPayload(StrictModel):
    webhook_ack_latency_ms: AckLatencyMetric
    duplicate_events_process_total: ProcessCounterMetric
    database_available: bool
    retries_total: int | None = Field(default=None, ge=0)
    dead_letter_count: int | None = Field(default=None, ge=0)
    enqueue_failed_count: int | None = Field(default=None, ge=0)
    provider_send_failures_15m: int | None = Field(default=None, ge=0)
    provider_status_failures_total: int | None = Field(default=None, ge=0)
    policy_blocks_15m: int | None = Field(default=None, ge=0)
    ai_escalations_15m: int | None = Field(default=None, ge=0)
    ai_decisions_15m: int | None = Field(default=None, ge=0)
    ai_escalation_rate_15m: float | None = Field(default=None, ge=0, le=1)
    opt_outs_total: int | None = Field(default=None, ge=0)
    oldest_takeover_queue_age_seconds: float | None = Field(default=None, ge=0)
    duplicate_send_invariant_breaches: int | None = Field(default=None, ge=0)
    kill_switches: list[KillSwitchMetric] = Field(max_length=MAX_CONTROL_STATES)
    kill_switches_truncated: bool
    kill_switch_active: bool
    redis_available: bool | None = None
    queue_depth: int | None = Field(default=None, ge=0)
    oldest_queue_age_seconds: float | None = Field(default=None, ge=0)
    worker_count: int | None = Field(default=None, ge=0)
    worker_heartbeat_age_seconds: float | None = Field(default=None, ge=0)


class MetricSnapshotResponse(StrictModel):
    scope: Literal["global", "tenant"]
    client_id: int | None
    generated_at: datetime
    metrics: MetricsPayload


class AlertRule(StrictModel):
    key: str
    metric: str
    operator: Literal["is_false", "is_true", "missing_or_greater_than", "greater_than", "greater_than_or_equal"]
    threshold: bool | float
    severity: Literal["high", "critical"]
    owner: str
    cooldown_seconds: int = Field(ge=0)


class ActiveAlert(AlertRule):
    current_value: Any
    active: Literal[True]
    evaluation_scope: Literal["stateless"]
    cooldown_state: Literal["not_tracked"]


class AlertRulesResponse(StrictModel):
    rules: list[AlertRule] = Field(max_length=MAX_ALERTS)


class AlertsResponse(StrictModel):
    generated_at: datetime
    alerts: list[ActiveAlert] = Field(max_length=MAX_ALERTS)


ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(key="worker_heartbeat_missing", metric="worker_heartbeat_age_seconds", operator="missing_or_greater_than", threshold=120, severity="critical", owner="platform-on-call", cooldown_seconds=300),
    AlertRule(key="queue_age_growing", metric="oldest_queue_age_seconds", operator="greater_than", threshold=300, severity="high", owner="messaging-on-call", cooldown_seconds=300),
    AlertRule(key="redis_unavailable", metric="redis_available", operator="is_false", threshold=False, severity="critical", owner="platform-on-call", cooldown_seconds=300),
    AlertRule(key="database_unavailable", metric="database_available", operator="is_false", threshold=False, severity="critical", owner="platform-on-call", cooldown_seconds=300),
    AlertRule(key="meta_send_failure_spike", metric="provider_send_failures_15m", operator="greater_than_or_equal", threshold=5, severity="high", owner="messaging-on-call", cooldown_seconds=900),
    AlertRule(key="duplicate_send_invariant_breach", metric="duplicate_send_invariant_breaches", operator="greater_than", threshold=0, severity="critical", owner="messaging-on-call", cooldown_seconds=3600),
    AlertRule(key="dead_letter_present", metric="dead_letter_count", operator="greater_than", threshold=0, severity="high", owner="messaging-on-call", cooldown_seconds=900),
    AlertRule(key="kill_switch_active", metric="kill_switch_active", operator="is_true", threshold=True, severity="high", owner="operations-on-call", cooldown_seconds=1800),
)


def _fires(rule: AlertRule, value: Any) -> bool:
    if rule.operator == "is_false":
        return value is False
    if rule.operator == "is_true":
        return value is True
    if rule.operator == "missing_or_greater_than":
        return value is None or value > rule.threshold
    if rule.operator == "greater_than":
        return value is not None and value > rule.threshold
    if rule.operator == "greater_than_or_equal":
        return value is not None and value >= rule.threshold
    return False


def evaluate_alerts(metrics: Mapping[str, Any]) -> list[ActiveAlert]:
    """Pure threshold evaluation; Phase 12B does not mutate delivery cooldowns."""
    return [ActiveAlert(**rule.model_dump(), current_value=metrics.get(rule.metric), active=True, evaluation_scope="stateless", cooldown_state="not_tracked") for rule in ALERT_RULES if _fires(rule, metrics.get(rule.metric))]

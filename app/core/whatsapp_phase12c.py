"""Pure Phase 12C contracts and provider-disabled drill definitions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_DEAD_LETTER_LIST = 100
MAX_REPLAY_BATCH = 10

ALERT_RUNBOOKS = {
    "worker_heartbeat_missing": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#worker-heartbeat-loss",
    "queue_age_growing": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#queue-depth-or-oldest-job-age-growth",
    "redis_unavailable": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#redis-outage",
    "database_unavailable": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#database-outage",
    "meta_send_failure_spike": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#meta-send-failure-spike",
    "duplicate_send_invariant_breach": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#duplicate-send-suspicion",
    "dead_letter_present": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#dead-letter-growth-and-replay",
    "kill_switch_active": "docs/runbooks/WHATSAPP_PHASE12_INCIDENTS.md#global-or-tenant-kill-switch-activation",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeadLetterItem(StrictModel):
    receipt_id: int
    event_kind: Literal["message", "status"]
    correlation_id: str
    state: Literal["dead_letter", "enqueue_failed", "replay_requested"]
    attempt_count: int = Field(ge=0)
    error_type: str | None
    received_at: datetime
    dead_lettered_at: datetime | None
    replay_eligible: bool


class DeadLetterListResponse(StrictModel):
    items: list[DeadLetterItem] = Field(max_length=MAX_DEAD_LETTER_LIST)
    limit: int = Field(ge=1, le=MAX_DEAD_LETTER_LIST)
    truncated: bool


class ReplayItem(StrictModel):
    receipt_id: int = Field(gt=0)
    original_correlation_id: UUID


class DeadLetterReplayBody(StrictModel):
    items: list[ReplayItem] = Field(min_length=1, max_length=MAX_REPLAY_BATCH)
    replay_limit: int = Field(ge=1, le=MAX_REPLAY_BATCH)
    reason: str = Field(min_length=1, max_length=255)


class ReplayOutcome(StrictModel):
    receipt_id: int
    correlation_id: str
    state: Literal["queued", "already_queued"]
    idempotent: bool


class DeadLetterReplayResponse(StrictModel):
    outcomes: list[ReplayOutcome] = Field(max_length=MAX_REPLAY_BATCH)


DRILL_STEPS: dict[str, tuple[str, ...]] = {
    "worker_pause_resume": (
        "Read the current worker-consumption control and record its version.",
        "Use the offline control fixture to simulate pause and confirm jobs remain queued.",
        "Simulate resume and confirm the same receipt is processed once.",
    ),
    "queue_backlog_alert": (
        "Evaluate the offline metric fixture with oldest queue age above threshold.",
        "Confirm queue_age_growing is active and contains no customer content.",
    ),
    "dead_letter_replay": (
        "Create a synthetic tenant receipt with fake identifiers.",
        "List it through the tenant-scoped service and replay with a bounded limit.",
        "Confirm the original correlation and dead-letter evidence remain present.",
    ),
    "kill_switch_activation": (
        "Evaluate a synthetic disabled switch state.",
        "Confirm outbound is blocked while inbound evidence remains persisted.",
    ),
    "alert_cooldown": (
        "Deliver a synthetic alert through the fake capture adapter.",
        "Repeat within cooldown and confirm suppression with the stable fingerprint.",
    ),
    "correlation_lookup": (
        "Use a synthetic correlation ID to locate receipt, intent, audit, and status records.",
        "Confirm no message body, phone, email, credential, or provider payload is printed.",
    ),
    "rollback_decision": (
        "Evaluate health, readiness, revision, worker, queue, and error signals from fixtures.",
        "Choose continue or rollback using the documented stop conditions only.",
    ),
}


def build_offline_drill(name: str) -> dict[str, object]:
    """Return a non-executing drill plan that cannot contact a provider."""
    if name not in DRILL_STEPS:
        raise ValueError("Unknown Phase 12C drill")
    return {
        "name": name,
        "mode": "offline",
        "provider_calls_enabled": False,
        "production_mutations_enabled": False,
        "steps": list(DRILL_STEPS[name]),
    }

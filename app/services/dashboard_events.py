"""Committed tenant-scoped lead events for dashboard SSE consumers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import AsyncIterator, Literal
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.orm import Session

from app.core.config import REDIS_URL
from app.core.models import Lead

logger = logging.getLogger(__name__)

DashboardEventType = Literal[
    "lead_created",
    "lead_updated",
    "lead_stage_changed",
]
EVENT_TYPES = frozenset({"lead_created", "lead_updated", "lead_stage_changed"})
_CHANNEL = "qualify:dashboard:lead-events:v1"
_PENDING_KEY = "dashboard_lead_events"
_HEARTBEAT_SECONDS = 15
_LOCAL_QUEUE_SIZE = 128


@dataclass(frozen=True)
class DashboardLeadEvent:
    event_id: str
    event_type: DashboardEventType
    client_id: int
    lead_id: int
    occurred_at: str
    previous_stage: str | None = None
    new_stage: str | None = None

    def public_payload(self) -> dict[str, object]:
        """Return only fields safe and necessary for client cache refresh."""
        payload: dict[str, object] = {
            "event_id": self.event_id,
            "lead_id": self.lead_id,
            "occurred_at": self.occurred_at,
        }
        if self.event_type == "lead_stage_changed":
            payload["previous_stage"] = self.previous_stage or ""
            payload["new_stage"] = self.new_stage or ""
        return payload


def new_dashboard_event(
    event_type: DashboardEventType,
    *,
    client_id: int,
    lead_id: int,
    previous_stage: str | None = None,
    new_stage: str | None = None,
) -> DashboardLeadEvent:
    if event_type not in EVENT_TYPES:
        raise ValueError("Unsupported dashboard event type")
    if client_id <= 0 or lead_id <= 0:
        raise ValueError("Dashboard events require tenant and lead IDs")
    return DashboardLeadEvent(
        event_id=uuid4().hex,
        event_type=event_type,
        client_id=client_id,
        lead_id=lead_id,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        previous_stage=previous_stage,
        new_stage=new_stage,
    )


def queue_dashboard_event(
    session: Session,
    event_type: DashboardEventType,
    *,
    client_id: int,
    lead_id: int,
    previous_stage: str | None = None,
    new_stage: str | None = None,
) -> DashboardLeadEvent:
    """Queue an event on a DB session; publish only after commit succeeds."""
    dashboard_event = new_dashboard_event(
        event_type,
        client_id=client_id,
        lead_id=lead_id,
        previous_stage=previous_stage,
        new_stage=new_stage,
    )
    pending = session.info.setdefault(_PENDING_KEY, [])
    for queued in pending:
        if (
            queued.event_type == event_type
            and queued.client_id == client_id
            and queued.lead_id == lead_id
        ):
            return queued
    pending.append(dashboard_event)
    return dashboard_event


class _LocalTenantBus:
    def __init__(self) -> None:
        self._lock = Lock()
        self._subscribers: dict[
            int,
            set[
                tuple[
                    asyncio.AbstractEventLoop,
                    asyncio.Queue[DashboardLeadEvent],
                ]
            ],
        ] = {}

    def subscribe(
        self, client_id: int
    ) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue[DashboardLeadEvent]]:
        subscription: tuple[
            asyncio.AbstractEventLoop, asyncio.Queue[DashboardLeadEvent]
        ] = (
            asyncio.get_running_loop(),
            asyncio.Queue(maxsize=_LOCAL_QUEUE_SIZE),
        )
        with self._lock:
            self._subscribers.setdefault(client_id, set()).add(subscription)
        return subscription

    def unsubscribe(
        self,
        client_id: int,
        subscription: tuple[
            asyncio.AbstractEventLoop, asyncio.Queue[DashboardLeadEvent]
        ],
    ) -> None:
        with self._lock:
            subscribers = self._subscribers.get(client_id)
            if not subscribers:
                return
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(client_id, None)

    def publish(self, dashboard_event: DashboardLeadEvent) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers.get(dashboard_event.client_id, ()))
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, dashboard_event)
            except RuntimeError:
                self.unsubscribe(dashboard_event.client_id, (loop, queue))

    @staticmethod
    def _offer(
        queue: asyncio.Queue[DashboardLeadEvent],
        dashboard_event: DashboardLeadEvent,
    ) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(dashboard_event)


_local_bus = _LocalTenantBus()
_redis_client = (
    Redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
        health_check_interval=30,
    )
    if REDIS_URL
    else None
)


def _publish_committed_event(
    dashboard_event: DashboardLeadEvent,
) -> None:
    """Publish without letting realtime availability affect committed data."""
    _local_bus.publish(dashboard_event)
    if _redis_client is None:
        return
    try:
        _redis_client.publish(
            _CHANNEL,
            json.dumps(asdict(dashboard_event), separators=(",", ":")),
        )
    except RedisError:
        logger.warning(
            "Dashboard event Redis publish failed",
            extra={
                "event": "dashboard_event_publish_failed",
                "client_id": dashboard_event.client_id,
                "event_type": dashboard_event.event_type,
            },
        )


@sqlalchemy_event.listens_for(Session, "after_flush")
def _queue_flushed_lead_events(session: Session, flush_context: object) -> None:
    """Capture every persisted Lead create/update without workflow bypasses."""
    del flush_context
    for lead in session.new:
        if isinstance(lead, Lead) and lead.id and lead.client_id:
            queue_dashboard_event(
                session,
                "lead_created",
                client_id=lead.client_id,
                lead_id=lead.id,
            )
    for lead in session.dirty:
        if (
            isinstance(lead, Lead)
            and lead.id
            and lead.client_id
            and session.is_modified(lead, include_collections=False)
        ):
            queue_dashboard_event(
                session,
                "lead_updated",
                client_id=lead.client_id,
                lead_id=lead.id,
            )


@sqlalchemy_event.listens_for(Session, "after_commit")
def _publish_session_events(session: Session) -> None:
    pending = session.info.pop(_PENDING_KEY, [])
    for dashboard_event in pending:
        _publish_committed_event(dashboard_event)


@sqlalchemy_event.listens_for(Session, "after_rollback")
def _discard_session_events(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def _decode_internal_event(raw: object) -> DashboardLeadEvent | None:
    try:
        data = json.loads(str(raw))
        event_type = data["event_type"]
        if event_type not in EVENT_TYPES:
            return None
        return DashboardLeadEvent(
            event_id=str(data["event_id"]),
            event_type=event_type,
            client_id=int(data["client_id"]),
            lead_id=int(data["lead_id"]),
            occurred_at=str(data["occurred_at"]),
            previous_stage=data.get("previous_stage"),
            new_stage=data.get("new_stage"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


async def iter_dashboard_events(
    client_id: int,
) -> AsyncIterator[DashboardLeadEvent | None]:
    """Yield only one tenant's events; None is an SSE heartbeat."""
    subscription = _local_bus.subscribe(client_id)
    _, local_queue = subscription
    pubsub = None
    if _redis_client is not None:
        try:
            pubsub = _redis_client.pubsub(ignore_subscribe_messages=True)
            await asyncio.to_thread(pubsub.subscribe, _CHANNEL)
        except RedisError:
            pubsub = None

    seen_order: deque[str] = deque(maxlen=256)
    seen: set[str] = set()
    idle_seconds = 0
    try:
        while True:
            dashboard_event = None
            try:
                dashboard_event = local_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

            if dashboard_event is None and pubsub is not None:
                try:
                    message = await asyncio.to_thread(
                        pubsub.get_message,
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )
                    if message and message.get("type") == "message":
                        candidate = _decode_internal_event(message.get("data"))
                        if candidate and candidate.client_id == client_id:
                            dashboard_event = candidate
                except RedisError:
                    await asyncio.to_thread(pubsub.close)
                    pubsub = None

            if dashboard_event is None and pubsub is None:
                try:
                    dashboard_event = await asyncio.wait_for(
                        local_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    pass

            if dashboard_event is not None:
                if dashboard_event.event_id in seen:
                    continue
                if len(seen_order) == seen_order.maxlen:
                    seen.discard(seen_order.popleft())
                seen_order.append(dashboard_event.event_id)
                seen.add(dashboard_event.event_id)
                idle_seconds = 0
                yield dashboard_event
                continue

            idle_seconds += 1
            if idle_seconds >= _HEARTBEAT_SECONDS:
                idle_seconds = 0
                yield None
    finally:
        _local_bus.unsubscribe(client_id, subscription)
        if pubsub is not None:
            await asyncio.to_thread(pubsub.close)

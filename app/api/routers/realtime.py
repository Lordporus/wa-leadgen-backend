"""Authenticated tenant-scoped dashboard server-sent events."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import verify_jwt
from app.core.models import Client
from app.services.dashboard_events import (
    DashboardLeadEvent,
    iter_dashboard_events,
)

router = APIRouter()


def _encode_event(dashboard_event: DashboardLeadEvent) -> str:
    data = json.dumps(
        dashboard_event.public_payload(), separators=(",", ":")
    )
    return (
        f"id: {dashboard_event.event_id}\n"
        f"event: {dashboard_event.event_type}\n"
        f"data: {data}\n\n"
    )


@router.get("/api/dashboard/events")
async def dashboard_events(
    request: Request,
    client: Client = Depends(verify_jwt),
) -> StreamingResponse:
    async def stream():
        yield "retry: 1000\n\n"
        async for dashboard_event in iter_dashboard_events(client.id):
            if await request.is_disconnected():
                break
            if dashboard_event is None:
                yield ": keep-alive\n\n"
            else:
                yield _encode_event(dashboard_event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

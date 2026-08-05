"""Protected tenant-scoped Phase 12C dead-letter operations."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.core.models import Client
from app.core.whatsapp_phase12c import (
    DeadLetterListResponse,
    DeadLetterReplayBody,
    DeadLetterReplayResponse,
    MAX_DEAD_LETTER_LIST,
)
from app.services import whatsapp_dead_letters

router = APIRouter(prefix="/api/whatsapp-operations/dead-letters", tags=["whatsapp-dead-letters"])


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, whatsapp_dead_letters.DeadLetterConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, whatsapp_dead_letters.DeadLetterError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail="WhatsApp dead-letter operations unavailable")


@router.get("", response_model=DeadLetterListResponse)
@limiter.limit("60/minute", key_func=get_client_key)
def list_dead_letters(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=MAX_DEAD_LETTER_LIST),
    client: Client = Depends(require_api_key),
):
    try:
        return whatsapp_dead_letters.list_dead_letters(client_id=client.id, limit=limit)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/replay", response_model=DeadLetterReplayResponse)
@limiter.limit("10/minute", key_func=get_client_key)
def replay_dead_letters(
    request: Request,
    response: Response,
    body: DeadLetterReplayBody,
    client: Client = Depends(require_api_key),
):
    try:
        return whatsapp_dead_letters.replay_dead_letters(
            client_id=client.id,
            items=[(item.receipt_id, str(item.original_correlation_id)) for item in body.items],
            replay_limit=body.replay_limit,
            actor=f"tenant:{client.id}:authenticated-session",
            reason=body.reason,
        )
    except Exception as exc:
        raise _http_error(exc) from exc

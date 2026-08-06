"""Protected tenant-scoped Phase 13 WhatsApp pilot operations."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.core.models import Client
from app.services import whatsapp_operations, whatsapp_pilot

router = APIRouter(prefix="/api/whatsapp-pilot", tags=["whatsapp-pilot"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PilotEnabledBody(StrictBody):
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=255)


class PilotStageBody(StrictBody):
    expected_stage: int = Field(ge=1, le=3)
    target_stage: int = Field(ge=1, le=3)
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=255)


def _context(request: Request, response: Response, client_id: int) -> tuple[str, str]:
    raw = request.headers.get("x-request-id", "")
    try:
        correlation_id = str(UUID(raw))
    except (ValueError, TypeError):
        correlation_id = str(uuid4())
    response.headers["X-Correlation-ID"] = correlation_id
    return correlation_id, f"tenant:{client_id}:authenticated-session"


def _error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (whatsapp_pilot.PilotConflict, whatsapp_operations.OperationalControlConflict),
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(
        exc,
        (whatsapp_pilot.PilotError, whatsapp_operations.OperationalControlError),
    ):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=503, detail="WhatsApp pilot controls unavailable")


@router.get("/status")
@limiter.limit("60/minute", key_func=get_client_key)
def get_status(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    try:
        return whatsapp_pilot.status(client_id=client.id)
    except Exception as exc:
        raise _error(exc) from exc


@router.put("/stage")
@limiter.limit("20/minute", key_func=get_client_key)
def transition_stage(
    request: Request,
    response: Response,
    body: PilotStageBody,
    client: Client = Depends(require_api_key),
):
    correlation_id, operator_id = _context(request, response, client.id)
    try:
        return whatsapp_pilot.transition_stage(
            client_id=client.id,
            expected_stage=body.expected_stage,
            target_stage=body.target_stage,
            expected_version=body.expected_version,
            operator_id=operator_id,
            reason=body.reason,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/stop")
@limiter.limit("20/minute", key_func=get_client_key)
def stop_pilot(
    request: Request,
    response: Response,
    body: PilotEnabledBody,
    client: Client = Depends(require_api_key),
):
    correlation_id, operator_id = _context(request, response, client.id)
    try:
        return whatsapp_pilot.set_enabled(
            client_id=client.id,
            enabled=False,
            expected_version=body.expected_version,
            operator_id=operator_id,
            reason=body.reason,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise _error(exc) from exc


@router.post("/resume")
@limiter.limit("10/minute", key_func=get_client_key)
def resume_pilot(
    request: Request,
    response: Response,
    body: PilotEnabledBody,
    client: Client = Depends(require_api_key),
):
    correlation_id, operator_id = _context(request, response, client.id)
    try:
        return whatsapp_pilot.set_enabled(
            client_id=client.id,
            enabled=True,
            expected_version=body.expected_version,
            operator_id=operator_id,
            reason=body.reason,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise _error(exc) from exc

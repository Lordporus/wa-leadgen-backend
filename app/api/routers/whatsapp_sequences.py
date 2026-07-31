"""Minimal tenant-scoped operations for Phase 8 WhatsApp sequences."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.core.models import Client
from app.services import whatsapp_sequences

router = APIRouter(prefix="/api/whatsapp-sequences", tags=["whatsapp-sequences"])


class StepBody(BaseModel):
    delay_seconds: int = Field(ge=0)
    template_id: int
    parameters: list | dict = Field(default_factory=list)


class CreateBody(BaseModel):
    name: str
    steps: list[StepBody]


class EditBody(BaseModel):
    name: str | None = None
    steps: list[StepBody] | None = None


class EnrollBody(BaseModel):
    lead_ids: list[int]


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, detail=str(exc)) from exc


@router.get("")
@limiter.limit("60/minute", key_func=get_client_key)
def list_sequences(
    request: Request, response: Response, client: Client = Depends(require_api_key)
):
    rows = _call(whatsapp_sequences.list_sequences, client.id)
    return {"sequences": rows, "count": len(rows)}


@router.post("")
@limiter.limit("20/minute", key_func=get_client_key)
def create_sequence(
    request: Request,
    response: Response,
    body: CreateBody,
    client: Client = Depends(require_api_key),
):
    return _call(
        whatsapp_sequences.create_sequence,
        client.id,
        body.name,
        [s.model_dump() for s in body.steps],
    )


@router.patch("/{sequence_id}/draft")
@limiter.limit("20/minute", key_func=get_client_key)
def edit_draft(
    request: Request,
    response: Response,
    sequence_id: int,
    body: EditBody,
    client: Client = Depends(require_api_key),
):
    steps = None if body.steps is None else [s.model_dump() for s in body.steps]
    return _call(
        whatsapp_sequences.edit_draft, client.id, sequence_id, body.name, steps
    )


@router.post("/{sequence_id}/enroll")
@limiter.limit("20/minute", key_func=get_client_key)
def enroll(
    request: Request,
    response: Response,
    sequence_id: int,
    body: EnrollBody,
    client: Client = Depends(require_api_key),
):
    return _call(whatsapp_sequences.enroll, client.id, sequence_id, body.lead_ids)


@router.get("/{sequence_id}/enrollments")
@limiter.limit("60/minute", key_func=get_client_key)
def list_enrollments(
    request: Request,
    response: Response,
    sequence_id: int,
    client: Client = Depends(require_api_key),
):
    return _call(whatsapp_sequences.list_enrollments, client.id, sequence_id)


@router.post("/{sequence_id}/{operation}")
@limiter.limit("30/minute", key_func=get_client_key)
def operate_sequence(
    request: Request,
    response: Response,
    sequence_id: int,
    operation: str,
    client: Client = Depends(require_api_key),
):
    if operation not in {"activate", "pause", "resume", "archive"}:
        raise HTTPException(404, detail="Unknown sequence operation")
    return _call(
        whatsapp_sequences.set_sequence_status, client.id, sequence_id, operation
    )


@router.post("/enrollments/{enrollment_id}/{operation}")
@limiter.limit("30/minute", key_func=get_client_key)
def operate_enrollment(
    request: Request,
    response: Response,
    enrollment_id: int,
    operation: str,
    client: Client = Depends(require_api_key),
):
    if operation not in {"pause", "resume", "cancel"}:
        raise HTTPException(404, detail="Unknown enrollment operation")
    return _call(
        whatsapp_sequences.set_enrollment_status, client.id, enrollment_id, operation
    )


@router.post("/enrollments/{enrollment_id}/dry-run")
@limiter.limit("60/minute", key_func=get_client_key)
def dry_run(
    request: Request,
    response: Response,
    enrollment_id: int,
    client: Client = Depends(require_api_key),
):
    return _call(whatsapp_sequences.dry_run, client.id, enrollment_id)

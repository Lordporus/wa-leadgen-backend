"""Protected Phase 12A WhatsApp operational-control APIs."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import (
    get_admin_key,
    get_client_key,
    limiter,
    require_admin,
    require_api_key,
)
from app.api.routers.admin import require_admin_secret
from app.core.models import Client
from app.services import whatsapp_operations

tenant_router = APIRouter(
    prefix="/api/whatsapp-operations",
    tags=["whatsapp-operations"],
)
admin_router = APIRouter(
    prefix="/api/admin/whatsapp-operations",
    tags=["whatsapp-operations-admin"],
)


class ControlMutationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control: str
    enabled: bool
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=255)
    resource_id: int | None = Field(default=None, gt=0)


def _operation_context(
    request: Request,
    response: Response,
    *,
    actor: str,
) -> tuple[str, str]:
    raw = request.headers.get("x-request-id", "")
    try:
        correlation_id = str(UUID(raw))
    except (ValueError, TypeError):
        correlation_id = str(uuid4())
    response.headers["X-Correlation-ID"] = correlation_id
    return correlation_id, actor


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, whatsapp_operations.OperationalControlConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, whatsapp_operations.OperationalControlError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(
        status_code=503, detail="WhatsApp operational controls unavailable"
    )


def _mutate(
    *,
    body: ControlMutationBody,
    client_id: int | None,
    operator_id: str,
    correlation_id: str,
) -> dict:
    try:
        state = whatsapp_operations.mutate(
            control=body.control,
            enabled_value=body.enabled,
            expected_version=body.expected_version,
            operator_id=operator_id,
            reason=body.reason,
            correlation_id=correlation_id,
            client_id=client_id,
            resource_id=body.resource_id,
        )
    except Exception as exc:
        raise _error(exc) from exc
    payload = state.as_dict()
    if (
        client_id is None
        and body.control == whatsapp_operations.WORKER_CONSUMPTION
    ):
        try:
            payload["runtime_applied"] = (
                whatsapp_operations.sync_worker_suspension(
                    enabled_value=body.enabled
                )
            )
        except Exception:
            payload["runtime_applied"] = False
    return payload


@tenant_router.get("/controls")
@limiter.limit("120/minute", key_func=get_client_key)
def tenant_controls(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    return {"controls": whatsapp_operations.list_states(client_id=client.id)}


@tenant_router.put("/controls")
@limiter.limit("30/minute", key_func=get_client_key)
def mutate_tenant_control(
    request: Request,
    response: Response,
    body: ControlMutationBody,
    client: Client = Depends(require_api_key),
):
    if body.control not in whatsapp_operations.TENANT_CONTROLS:
        raise HTTPException(status_code=422, detail="Unknown tenant control")
    correlation_id, operator_id = _operation_context(
        request,
        response,
        actor=f"tenant:{client.id}:authenticated-session",
    )
    return _mutate(
        body=body,
        client_id=client.id,
        operator_id=operator_id,
        correlation_id=correlation_id,
    )


@admin_router.get(
    "/controls",
    dependencies=[Depends(require_admin_secret)],
)
@limiter.limit("60/minute", key_func=get_admin_key)
def global_controls(
    request: Request,
    response: Response,
    admin: Client = Depends(require_admin),
):
    return {"controls": whatsapp_operations.list_states(client_id=None)}


@admin_router.put(
    "/controls",
    dependencies=[Depends(require_admin_secret)],
)
@limiter.limit("20/minute", key_func=get_admin_key)
def mutate_global_control(
    request: Request,
    response: Response,
    body: ControlMutationBody,
    admin: Client = Depends(require_admin),
):
    if body.control not in whatsapp_operations.GLOBAL_CONTROLS:
        raise HTTPException(status_code=422, detail="Unknown global control")
    correlation_id, operator_id = _operation_context(
        request,
        response,
        actor=f"admin:{admin.id}:authenticated-session",
    )
    return _mutate(
        body=body,
        client_id=None,
        operator_id=operator_id,
        correlation_id=correlation_id,
    )


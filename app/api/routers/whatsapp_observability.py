"""Protected Phase 12B WhatsApp operational visibility APIs."""

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_admin_key, get_client_key, limiter, require_admin, require_api_key
from app.api.routers.admin import require_admin_secret
from app.core.models import Client
from app.core.whatsapp_observability import AlertRulesResponse, AlertsResponse, MetricSnapshotResponse
from app.services import whatsapp_observability

tenant_router = APIRouter(prefix="/api/whatsapp-observability", tags=["whatsapp-observability"])
admin_router = APIRouter(prefix="/api/admin/whatsapp-observability", tags=["whatsapp-observability-admin"])


@tenant_router.get("/metrics", response_model=MetricSnapshotResponse)
@limiter.limit("60/minute", key_func=get_client_key)
def tenant_metrics(request: Request, response: Response, client: Client = Depends(require_api_key)):
    return whatsapp_observability.collect_metrics(client_id=client.id, include_infrastructure=False)


@admin_router.get("/metrics", response_model=MetricSnapshotResponse, dependencies=[Depends(require_admin_secret)])
@limiter.limit("60/minute", key_func=get_admin_key)
def global_metrics(request: Request, response: Response, admin: Client = Depends(require_admin)):
    return whatsapp_observability.collect_metrics(client_id=None, include_infrastructure=True)


@admin_router.get("/alert-rules", response_model=AlertRulesResponse, dependencies=[Depends(require_admin_secret)])
@limiter.limit("60/minute", key_func=get_admin_key)
def alert_rules(request: Request, response: Response, admin: Client = Depends(require_admin)):
    return whatsapp_observability.alert_rules_payload()


@admin_router.get("/alerts", response_model=AlertsResponse, dependencies=[Depends(require_admin_secret)])
@limiter.limit("60/minute", key_func=get_admin_key)
def active_alerts(request: Request, response: Response, admin: Client = Depends(require_admin)):
    return whatsapp_observability.active_alerts_payload()

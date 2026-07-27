from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.core.models import Client

router = APIRouter()

class CampaignStepBody(BaseModel):
    delay_hours: int = 0
    subject_template: str
    body_template: str


class CampaignCreateBody(BaseModel):
    name: str
    steps: list[CampaignStepBody] | None = None


class CampaignUpdateBody(BaseModel):
    name: str | None = None
    status: str | None = None  # draft | active | paused | archived


class CampaignStepsBody(BaseModel):
    steps: list[CampaignStepBody]


class CampaignEnrollBody(BaseModel):
    lead_ids: list[int]


@router.get("/api/campaigns")
@limiter.limit("60/minute", key_func=get_client_key)
def list_email_campaigns(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    rows = camp.list_campaigns(client.id)
    return {"campaigns": rows, "count": len(rows)}


@router.post("/api/campaigns")
@limiter.limit("20/minute", key_func=get_client_key)
def create_email_campaign(
    request: Request,
    response: Response,
    body: CampaignCreateBody,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    steps = None
    if body.steps:
        steps = [s.model_dump() for s in body.steps]
    try:
        created = camp.create_campaign(client.id, body.name, steps=steps)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return created


@router.get("/api/campaigns/{campaign_id}")
@limiter.limit("60/minute", key_func=get_client_key)
def get_email_campaign(
    request: Request,
    response: Response,
    campaign_id: int,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    row = camp.get_campaign(client.id, campaign_id)
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return row


@router.patch("/api/campaigns/{campaign_id}")
@limiter.limit("30/minute", key_func=get_client_key)
def update_email_campaign(
    request: Request,
    response: Response,
    campaign_id: int,
    body: CampaignUpdateBody,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    try:
        return camp.update_campaign(
            client.id, campaign_id, name=body.name, status=body.status
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Campaign not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/api/campaigns/{campaign_id}/steps")
@limiter.limit("20/minute", key_func=get_client_key)
def put_email_campaign_steps(
    request: Request,
    response: Response,
    campaign_id: int,
    body: CampaignStepsBody,
    client: Client = Depends(require_api_key),
):
    """Replace all steps (campaign must not be active)."""
    from app.email import email_campaigns as camp

    try:
        return camp.set_campaign_steps(
            client.id, campaign_id, [s.model_dump() for s in body.steps]
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Campaign not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/campaigns/{campaign_id}/enroll")
@limiter.limit("20/minute", key_func=get_client_key)
def enroll_email_campaign(
    request: Request,
    response: Response,
    campaign_id: int,
    body: CampaignEnrollBody,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    try:
        return camp.enroll_leads(client.id, campaign_id, body.lead_ids)
    except LookupError:
        raise HTTPException(status_code=404, detail="Campaign not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/api/campaigns/{campaign_id}/enrollments")
@limiter.limit("60/minute", key_func=get_client_key)
def list_email_campaign_enrollments(
    request: Request,
    response: Response,
    campaign_id: int,
    client: Client = Depends(require_api_key),
):
    """List enrollments for a campaign (lead name/email + status for pause/resume UI)."""
    from app.email import email_campaigns as camp

    try:
        return camp.list_enrollments(client.id, campaign_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Campaign not found")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/api/campaigns/enrollments/{enrollment_id}/pause")
@limiter.limit("30/minute", key_func=get_client_key)
def pause_campaign_enrollment(
    request: Request,
    response: Response,
    enrollment_id: int,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    try:
        return camp.pause_enrollment(client.id, enrollment_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/campaigns/enrollments/{enrollment_id}/resume")
@limiter.limit("30/minute", key_func=get_client_key)
def resume_campaign_enrollment(
    request: Request,
    response: Response,
    enrollment_id: int,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    try:
        return camp.resume_enrollment(client.id, enrollment_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/api/campaigns/{campaign_id}/analytics")
@limiter.limit("60/minute", key_func=get_client_key)
def email_campaign_analytics(
    request: Request,
    response: Response,
    campaign_id: int,
    client: Client = Depends(require_api_key),
):
    from app.email import email_campaigns as camp

    try:
        return camp.campaign_analytics(client.id, campaign_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Campaign not found")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


# ── API key rotation ──────────────────────────────────────────────────────

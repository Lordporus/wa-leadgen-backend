import hashlib
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from app.api.dependencies import get_client_key, limiter, require_api_key
from app.api.runtime import logger
from app.core.database import SessionLocal
from app.core.models import Client, PromptTemplate

router = APIRouter()
account_router = APIRouter()

class PipelineStageUpdate(BaseModel):
    id: int
    name: str
    is_won: bool
    is_lost: bool

class SettingsUpdateBody(BaseModel):
    system_prompt: str | None = None
    calendly_link: str | None = None
    wa_phone_number_id: str | None = None
    pipeline_stages: list[PipelineStageUpdate] | None = None
    brand_color: str | None = None
    logo_url: str | None = None
    company_display_name: str | None = None
    hot_lead_threshold: int | None = None


@router.get("/api/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def get_settings(request: Request, response: Response, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        return {"system_prompt": "", "calendly_link": "", "wa_phone_number_id": "", "pipeline_stages": [], "brand_color": "#C8A96E", "logo_url": "", "company_display_name": "Leadgen CRM", "hot_lead_threshold": 70}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            return {"system_prompt": "", "calendly_link": "", "wa_phone_number_id": "", "pipeline_stages": [], "brand_color": "#C8A96E", "logo_url": "", "company_display_name": "Leadgen CRM", "hot_lead_threshold": 70}

        stage_list = [{"id": st.id, "name": st.name, "position": st.position, "is_won": st.is_won, "is_lost": st.is_lost} for st in db_client.pipeline_stages]

        return {
            "system_prompt": db_client.system_prompt or "",
            "calendly_link": db_client.calendly_link or "",
            "wa_phone_number_id": db_client.wa_phone_number_id or "",
            "pipeline_stages": stage_list,
            "brand_color": db_client.brand_color or "#10B981",
            "logo_url": db_client.logo_url or "",
            "company_display_name": db_client.company_display_name or db_client.name or "Leadgen CRM",
            "hot_lead_threshold": db_client.hot_lead_threshold if db_client.hot_lead_threshold is not None else 70,
        }

@router.patch("/api/settings")
@limiter.limit("120/minute", key_func=get_client_key)
def update_settings(request: Request, response: Response, body: SettingsUpdateBody, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # Validate hex color before any DB work (mirrors POST /api/settings/branding).
    if body.brand_color is not None and not _HEX_COLOR_RE.match(body.brand_color.strip()):
        raise HTTPException(
            status_code=400,
            detail="brand_color must be a valid hex color, e.g. '#C8A96E' or '#FFF'",
        )

    # Validate threshold is within 0-100.
    if body.hot_lead_threshold is not None and not (0 <= body.hot_lead_threshold <= 100):
        raise HTTPException(
            status_code=400,
            detail="hot_lead_threshold must be an integer between 0 and 100",
        )

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        if body.system_prompt is not None:
            db_client.system_prompt = body.system_prompt
        if body.calendly_link is not None:
            db_client.calendly_link = body.calendly_link
        if body.wa_phone_number_id is not None:
            phone_number_id = body.wa_phone_number_id.strip()
            if not phone_number_id:
                raise HTTPException(status_code=400, detail="wa_phone_number_id must not be blank")
            if db_client.is_active:
                already_assigned = (
                    s.query(Client.id)
                    .filter(
                        Client.wa_phone_number_id == phone_number_id,
                        Client.is_active.is_(True),
                        Client.id != db_client.id,
                    )
                    .first()
                )
                if already_assigned:
                    raise HTTPException(
                        status_code=409,
                        detail="wa_phone_number_id is already assigned to an active tenant",
                    )
            db_client.wa_phone_number_id = phone_number_id
        if body.brand_color is not None:
            db_client.brand_color = body.brand_color.strip()
        if body.logo_url is not None:
            db_client.logo_url = body.logo_url
        if body.company_display_name is not None:
            db_client.company_display_name = body.company_display_name
        if body.hot_lead_threshold is not None:
            db_client.hot_lead_threshold = body.hot_lead_threshold

        if body.pipeline_stages is not None:
            stage_map = {st.id: st for st in db_client.pipeline_stages}
            for stage_update in body.pipeline_stages:
                if stage_update.id in stage_map:
                    stage = stage_map[stage_update.id]
                    stage.name = stage_update.name
                    stage.is_won = stage_update.is_won
                    stage.is_lost = stage_update.is_lost

        s.commit()

    return {"success": True}

@router.get("/api/pipeline-stages")
@limiter.limit("120/minute", key_func=get_client_key)
def get_pipeline_stages(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """
    Dedicated read endpoint for the tenant's ordered pipeline stages.

    Frontend `useStages()` reads from here (previously it piggy-backed on
    GET /api/settings). Same row shape as the `pipeline_stages` block that
    /api/settings returns, ordered by position.
    """
    if not SessionLocal:
        return {"pipeline_stages": []}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            return {"pipeline_stages": []}
        stage_list = [
            {"id": st.id, "name": st.name, "position": st.position, "is_won": st.is_won, "is_lost": st.is_lost}
            for st in db_client.pipeline_stages
        ]
        return {"pipeline_stages": stage_list}

# ── Sprint 9: White-label branding endpoints ───────────────────────────────

# Accepts #RGB or #RRGGBB (case-insensitive). Anchored so no extra chars slip in.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

class BrandingUpdateBody(BaseModel):
    brand_color: str | None = None
    logo_url: str | None = None
    company_display_name: str | None = None


@router.get("/api/settings/branding")
@limiter.limit("120/minute", key_func=get_client_key)
def get_branding(request: Request, response: Response, client: Client = Depends(require_api_key)):
    """Return the tenant's white-label branding fields (theme customization)."""
    if not SessionLocal:
        return {"brand_color": "#C8A96E", "logo_url": "", "company_display_name": "Leadgen CRM"}
    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")
        return {
            "brand_color": db_client.brand_color or "#C8A96E",
            "logo_url": db_client.logo_url or "",
            "company_display_name": db_client.company_display_name or db_client.name or "Leadgen CRM",
        }


@router.post("/api/settings/branding")
@limiter.limit("60/minute", key_func=get_client_key)
def update_branding(request: Request, response: Response, body: BrandingUpdateBody, client: Client = Depends(require_api_key)):
    """
    Update the tenant's white-label branding. All fields optional (partial
    update). brand_color, when supplied, must be a valid #RGB / #RRGGBB hex
    string or the request is rejected 400.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # ── Validate hex color before touching the DB ─────────────────────
    if body.brand_color is not None:
        color = body.brand_color.strip()
        if not _HEX_COLOR_RE.match(color):
            raise HTTPException(
                status_code=400,
                detail="brand_color must be a valid hex color, e.g. '#C8A96E' or '#FFF'",
            )
    else:
        color = None

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        if color is not None:
            db_client.brand_color = color
        if body.logo_url is not None:
            db_client.logo_url = body.logo_url
        if body.company_display_name is not None:
            db_client.company_display_name = body.company_display_name

        s.commit()

        return {
            "success": True,
            "brand_color": db_client.brand_color or "#C8A96E",
            "logo_url": db_client.logo_url or "",
            "company_display_name": db_client.company_display_name or db_client.name or "Leadgen CRM",
        }


# ── Email outreach (Phase E2: settings + single send) ─────────────────────

_BLOCKED_EMAIL_STATUSES = frozenset({"bounced", "complained", "unsubscribed"})
_MAX_EMAIL_SUBJECT_LEN = 500
_MAX_EMAIL_BODY_LEN = 100_000



@account_router.post("/api/settings/regenerate-api-key")
@limiter.limit("3/minute", key_func=get_client_key)
def regenerate_api_key(
    request: Request,
    response: Response,
    client: Client = Depends(require_api_key),
):
    """
    Rotate the authenticated tenant's dashboard API key.

    The caller is already authenticated via require_api_key (Bearer token in
    the Authorization header), which is sufficient proof of key ownership —
    no second factor is required for a beta product.

    Key generation reuses the identical pattern used in onboard_client.py and
    the admin /admin/create-client endpoint:
        raw_key = secrets.token_hex(32)   # 256 bits of CSPRNG entropy
        key_hash = sha256(raw_key)        # stored; never the raw value

    The raw key is returned in the response body EXACTLY ONCE and is never
    written to any log. After this call the old key is immediately invalid.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    # ── Generate new key (same CSPRNG pattern as onboard_client.py) ──────
    new_raw_key = secrets.token_hex(32)
    new_key_hash = hashlib.sha256(new_raw_key.encode("utf-8")).hexdigest()

    with SessionLocal() as s:
        db_client = s.query(Client).filter(Client.id == client.id).first()
        if not db_client:
            raise HTTPException(status_code=404, detail="Client not found")

        db_client.dashboard_api_key_hash = new_key_hash
        s.commit()

    # Log the rotation event — client_id and timestamp only, never the key.
    logger.info(
        "API key rotated: client_id=%s at=%s",
        client.id,
        datetime.utcnow().isoformat(),
    )

    # Return the raw key once. The caller must copy it immediately.
    return {
        "success": True,
        "api_key": new_raw_key,
        "note": "Copy this key now — it will not be shown again.",
    }


# ── Template library endpoints ────────────────────────────────────────────

@account_router.get("/api/templates", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def list_templates(request: Request, response: Response):
    if not SessionLocal:
        return []
    with SessionLocal() as s:
        templates = s.query(PromptTemplate).order_by(PromptTemplate.id).all()
        return [
            {"slug": t.slug, "niche": t.niche, "display_name": t.display_name, "is_default": t.is_default}
            for t in templates
        ]

@account_router.get("/api/templates/{slug}", dependencies=[Depends(require_api_key)])
@limiter.limit("120/minute", key_func=get_client_key)
def get_template(request: Request, response: Response, slug: str, client: Client = Depends(require_api_key)):
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")
    with SessionLocal() as s:
        template = s.query(PromptTemplate).filter(PromptTemplate.slug == slug).first()
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")
        body = template.body
        body = body.replace("{{agency_name}}", client.company_display_name or client.name or "Our Agency")
        body = body.replace("{{calendly_link}}", client.calendly_link or "")
        return {"slug": template.slug, "niche": template.niche, "display_name": template.display_name, "body": body}

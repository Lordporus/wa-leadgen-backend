import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

from app.api.dependencies import (
    get_admin_key,
    limiter,
    require_admin,
)
from app.api.runtime import logger
from app.core.config import ADMIN_SECRET
from app.core.database import SessionLocal
from app.core.models import Client, PipelineStage

router = APIRouter()

class AdminCreateClientBody(BaseModel):
    name: str
    wa_phone_number_id: str
    system_prompt: str | None = None
    calendly_link: str | None = None
    followup_template: str | None = None
    admin_note: str | None = None

# ── F6: Admin onboarding endpoint ─────────────────────────────────────────
_admin_secret_header = APIKeyHeader(name="X-Admin-Secret", auto_error=False)

def require_admin_secret(secret: str = Security(_admin_secret_header)):
    """Dependency: fail closed — rejects if ADMIN_SECRET is unset OR mismatched."""
    if not ADMIN_SECRET:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET is not configured on the server. Please check environment variables.",
        )
    if not secret or not hmac.compare_digest(secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing admin secret")


@router.post("/api/admin/clients", dependencies=[Depends(require_admin_secret), Depends(require_admin)])
@limiter.limit("10/minute", key_func=get_admin_key)
def admin_create_client(request: Request, response: Response, body: AdminCreateClientBody):
    """
    Onboard a new client.

    Creates the client row, generates a dashboard API key (returned once),
    and seeds default pipeline stages.
    """
    if not SessionLocal:
        raise HTTPException(status_code=500, detail="Database not configured")

    with SessionLocal() as s:
        # ── a. Check wa_phone_number_id uniqueness ────────────────────
        existing = (
            s.query(Client)
            .filter(Client.wa_phone_number_id == body.wa_phone_number_id)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"wa_phone_number_id '{body.wa_phone_number_id}' is already "
                    f"assigned to client id={existing.id} ({existing.name!r})"
                ),
            )

        # ── b. Generate raw API key ───────────────────────────────────
        raw_api_key = secrets.token_hex(32)

        # ── c. Compute hash for storage ───────────────────────────────
        key_hash = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()

        # ── d. Insert client row ──────────────────────────────────────
        new_client = Client(
            name=body.name,
            wa_phone_number_id=body.wa_phone_number_id,
            system_prompt=body.system_prompt,
            calendly_link=body.calendly_link,
            followup_template=body.followup_template or "",
            dashboard_api_key_hash=key_hash,
            is_active=True,
            admin_note=body.admin_note,
            plan_tier="base",
            subscription_status="inactive",
        )
        s.add(new_client)
        s.flush()  # get the auto-generated id before seeding stages

        # ── e. Seed default pipeline stages ───────────────────────────
        default_stages = [
            ("New Lead",  1, False, False),
            ("Contacted", 2, False, False),
            ("Qualified", 3, False, False),
            ("Booked",    4, True,  False),
            ("Lost",      5, False, True),
        ]
        for stage_name, position, is_won, is_lost in default_stages:
            s.add(PipelineStage(
                client_id=new_client.id,
                name=stage_name,
                position=position,
                is_won=is_won,
                is_lost=is_lost,
            ))

        s.commit()

    logger.info(
        f"F6: onboarded client id={new_client.id} name={body.name!r} "
        f"wa_phone={body.wa_phone_number_id}"
    )

    # ── f. Return credentials (raw key shown only once) ───────────
    return {
        "client_id": new_client.id,
        "name": new_client.name,
        "dashboard_api_key": raw_api_key,
        "wa_phone_number_id": new_client.wa_phone_number_id,
        "pipeline_stages_seeded": len(default_stages),
        "plan_tier": "base",
        "subscription_status": "inactive",
    }


# ── Dashboard helper utilities ─────────────────────────────────────────────

DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

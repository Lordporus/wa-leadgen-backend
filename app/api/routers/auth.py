from datetime import datetime, timedelta

import jwt as pyjwt
from fastapi import APIRouter, HTTPException, Request, Response

from app.api.dependencies import (
    LoginBody,
    _JWT_ALGORITHM,
    _JWT_EXPIRY_HOURS,
    limiter,
)
from app.core.config import JWT_SECRET
from app.services import tenant

router = APIRouter()

@router.post("/auth/login")
@limiter.limit("10/minute")
def login(request: Request, response: Response, body: LoginBody):
    """
    Authenticate with a client API key and receive a signed JWT.
    The raw API key is validated against the hashed key in the clients table.
    """
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured on the server")

    ctx = tenant.resolve_context_by_api_key(body.api_key)
    if not ctx:
        raise HTTPException(status_code=401, detail="Invalid API key")

    now = datetime.utcnow()
    payload = {
        "client_id": ctx.client.id,
        "tenant_id": ctx.client.id,
        "role": "admin",
        "iat": now,
        "exp": now + timedelta(hours=_JWT_EXPIRY_HOURS),
    }
    token = pyjwt.encode(payload, JWT_SECRET, algorithm=_JWT_ALGORITHM)

    return {"access_token": token, "token_type": "bearer", "expires_in": _JWT_EXPIRY_HOURS * 3600}

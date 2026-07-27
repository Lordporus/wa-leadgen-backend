from fastapi import HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import JWT_SECRET
from app.core.models import Client
from app.services import tenant


def get_client_key(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    ip = get_remote_address(request)
    return f"client:{api_key}" if api_key else ip


def get_admin_key(request: Request) -> str:
    admin_secret = request.headers.get("X-Admin-Secret")
    ip = get_remote_address(request)
    return f"admin:{admin_secret}:{ip}" if admin_secret else ip


limiter = Limiter(key_func=get_remote_address, headers_enabled=True)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str = Security(_api_key_header)) -> Client:
    """
    Dependency: resolve client from API key, with grace-period fallback.
    """
    if not api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")

    ctx = tenant.resolve_context_by_api_key(api_key)
    if ctx:
        return ctx.client

    raise HTTPException(status_code=403, detail="Invalid or missing API key")


import jwt as pyjwt

_JWT_ALGORITHM = "HS256"
_JWT_EXPIRY_HOURS = 24


class LoginBody(BaseModel):
    api_key: str


def verify_jwt(request: Request) -> Client:
    """
    Dependency: decode and verify a JWT from the Authorization header.
    Returns the Client ORM row for the authenticated tenant.
    Fails closed: missing/invalid/expired token → 401.
    """
    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured on the server")

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = auth_header.split(" ", 1)[1]
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    client_id = payload.get("client_id")
    if not client_id:
        raise HTTPException(status_code=401, detail="Token missing client_id")

    client = tenant.load_client(client_id)
    if not client or not client.is_active:
        raise HTTPException(status_code=401, detail="Client not found or inactive")

    return client


def require_admin(request: Request) -> Client:
    """
    Dependency: verify JWT and enforce role == "admin".
    Returns the Client ORM row. Raises 403 if the token is valid but
    the role is not admin.
    """
    client = verify_jwt(request)

    if not JWT_SECRET:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured on the server")

    auth_header = request.headers.get("Authorization", "")
    token = auth_header.split(" ", 1)[1]
    payload = pyjwt.decode(token, JWT_SECRET, algorithms=[_JWT_ALGORITHM])

    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")

    return client

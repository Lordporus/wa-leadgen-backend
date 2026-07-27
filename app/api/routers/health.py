from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.api.dependencies import limiter
from app.api.runtime import redis_conn
from app.core.database import SessionLocal

router = APIRouter()

@router.get("/")
@limiter.limit("60/minute")
def read_root(request: Request, response: Response):
    return {"status": "ok", "message": "WhatsApp Acquisition System is running."}


@router.get("/health")
@limiter.limit("60/minute")
def health_check(request: Request, response: Response):
    """Infrastructure health check — no auth required."""
    db_ok = False
    redis_ok = False

    if SessionLocal:
        try:
            with SessionLocal() as s:
                s.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            pass

    if redis_conn:
        try:
            redis_conn.ping()
            redis_ok = True
        except Exception:
            pass

    status = "ok" if db_ok else "degraded"
    return {"status": status, "db": db_ok, "redis": redis_ok}

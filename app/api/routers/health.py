from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, HTTPException, Request, Response
from rq import Worker
from sqlalchemy import text

from app.api.dependencies import limiter
from app.api.runtime import redis_conn
from app.api.runtime import webhook_queue
from app.core.config import (
    DATABASE_URL,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_APP_SECRET,
    WHATSAPP_OUTBOUND_ENABLED,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_RQ_CONSUMER_ENABLED,
    WHATSAPP_RQ_QUEUE,
)
from app.core.database import SessionLocal

router = APIRouter()


def _required_schema_revision() -> str:
    """Return the single Alembic head required by this application checkout."""
    config_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    heads = ScriptDirectory.from_config(Config(str(config_path))).get_heads()
    if len(heads) != 1:
        raise RuntimeError("Alembic must have exactly one configured head")
    return heads[0]


def _schema_readiness() -> dict[str, str | bool | None]:
    """Compare the live Alembic revision with this checkout without leaking URLs."""
    required_revision: str | None = None
    current_revision: str | None = None
    try:
        required_revision = _required_schema_revision()
        if not SessionLocal:
            raise RuntimeError("database session is unavailable")
        with SessionLocal() as session:
            current_revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
    except Exception:
        return {"ready": False, "required_revision": required_revision, "current_revision": None}
    return {
        "ready": current_revision == required_revision,
        "required_revision": required_revision,
        "current_revision": current_revision,
    }

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

    queue_ok = False
    queue_depth = None
    worker_count = 0
    if redis_ok and webhook_queue is not None:
        try:
            queue_depth = webhook_queue.count
            workers = Worker.all(connection=redis_conn)
            worker_count = sum(WHATSAPP_RQ_QUEUE in worker.queue_names() for worker in workers)
            queue_ok = worker_count > 0
        except Exception:
            pass

    status = "ok" if db_ok and redis_ok and queue_ok else "degraded"
    return {
        "status": status,
        "db": db_ok,
        "redis": redis_ok,
        "whatsapp_queue": {
            "ready": queue_ok,
            "depth": queue_depth,
            "workers": worker_count,
        },
    }


@router.get("/ready")
@limiter.limit("60/minute")
def readiness_check(request: Request, response: Response):
    """Fail closed for rollout traffic without exposing configuration values."""
    health = health_check(request, response)
    schema = _schema_readiness()
    configuration = {
        "database_url_set": bool(DATABASE_URL),
        "whatsapp_access_token_set": bool(WHATSAPP_ACCESS_TOKEN),
        "whatsapp_phone_number_id_set": bool(WHATSAPP_PHONE_NUMBER_ID),
        "whatsapp_app_secret_set": bool(WHATSAPP_APP_SECRET),
        "queue_consumer_enabled": WHATSAPP_RQ_CONSUMER_ENABLED,
        "outbound_enabled": WHATSAPP_OUTBOUND_ENABLED,
    }
    ready = health["status"] == "ok" and schema["ready"] and all(
        value for key, value in configuration.items() if key != "outbound_enabled"
    )
    payload = {
        **health,
        "status": "ready" if ready else "not_ready",
        "schema": schema,
        "configuration": configuration,
    }
    if not ready:
        raise HTTPException(status_code=503, detail=payload)
    return payload

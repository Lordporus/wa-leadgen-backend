from fastapi import APIRouter, Request, Response
from rq import Worker
from sqlalchemy import text

from app.api.dependencies import limiter
from app.api.runtime import redis_conn
from app.api.runtime import webhook_queue
from app.core.config import WHATSAPP_RQ_QUEUE
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

"""
RQ worker entry point.

Connects to Redis and processes jobs from the 'webhooks' queue.

Usage:
    cd backend
    python worker.py

Graceful shutdown: the worker traps SIGTERM, finishes the current job,
then exits cleanly (per docs/11_INFRASTRUCTURE_SPECIFICATION.md §20).
"""

import logging
import signal
import time
from redis import Redis
from rq import Worker, Queue

from app.core.config import (
    REDIS_URL,
    WHATSAPP_RQ_CONSUMER_ENABLED,
    WHATSAPP_RQ_QUEUE,
    WHATSAPP_RQ_WORKER_CONCURRENCY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

QUEUE_NAME = WHATSAPP_RQ_QUEUE
SEQUENCE_TICK_JOB_ID = "whatsapp-sequence-tick"


def validate_worker_configuration() -> None:
    """RQ Worker is single-concurrency; scale by explicit worker instances."""
    if WHATSAPP_RQ_WORKER_CONCURRENCY != 1:
        raise RuntimeError(
            "WHATSAPP_RQ_WORKER_CONCURRENCY must be 1; RQ workers process one job at a time"
        )


def ensure_sequence_tick(queue: Queue) -> None:
    """Seed the singleton Phase 8 scheduler only when no tick is pending."""
    existing = queue.fetch_job(SEQUENCE_TICK_JOB_ID)
    if existing is not None and existing.get_status() in {
        "queued",
        "started",
        "scheduled",
        "deferred",
    }:
        return
    from app.services.whatsapp_sequences import run_sequence_tick_job

    queue.enqueue(run_sequence_tick_job, job_id=SEQUENCE_TICK_JOB_ID)


def main():
    validate_worker_configuration()
    logger.info(f"Connecting to Redis at {REDIS_URL}")
    conn = Redis.from_url(REDIS_URL)
    conn.ping()
    logger.info("Redis connection OK")

    queue = Queue(QUEUE_NAME, connection=conn)
    try:
        ensure_sequence_tick(queue)
    except Exception as exc:
        logger.info(
            "Sequence tick already queued or unavailable: %s", type(exc).__name__
        )
    # RQ's worker installs SIGTERM handling: it stops accepting new jobs,
    # completes the active job, and leaves queued work durable in Redis.
    worker = Worker([queue], connection=conn, default_worker_ttl=420)

    if not WHATSAPP_RQ_CONSUMER_ENABLED:
        logger.warning(
            "WhatsApp RQ consumer disabled; accepted webhook jobs remain durable in Redis"
        )
        stopping = False

        def _stop_consumer(_signal, _frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, _stop_consumer)
        signal.signal(signal.SIGINT, _stop_consumer)
        while not stopping:
            time.sleep(5)
        logger.info("Disabled WhatsApp RQ consumer stopped")
        return

    logger.info(f"Starting RQ worker on queue '{QUEUE_NAME}'")
    worker.work(with_scheduler=True, max_jobs=None)


if __name__ == "__main__":
    main()

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
import sys

from redis import Redis
from rq import Worker, Queue

from config import REDIS_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

QUEUE_NAME = "webhooks"


def main():
    logger.info(f"Connecting to Redis at {REDIS_URL}")
    conn = Redis.from_url(REDIS_URL)
    conn.ping()
    logger.info("Redis connection OK")

    queue = Queue(QUEUE_NAME, connection=conn)
    worker = Worker([queue], connection=conn)

    logger.info(f"Starting RQ worker on queue '{QUEUE_NAME}'")
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()

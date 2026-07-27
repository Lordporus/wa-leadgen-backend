import logging

from redis import Redis
from rq import Queue as RQQueue

from app.clients.calendly_client import CalendlyClient
from app.clients.whatsapp_client import WhatsAppClient
from app.core.config import (
    REDIS_URL,
    SENTRY_DSN,
    SENTRY_ENVIRONMENT,
    SENTRY_TRACES_SAMPLE_RATE,
)
from app.store.store import get_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

# Initialize observability before provider clients, matching the previous
# main.py import order.
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        send_default_pii=False,
    )
    logger.info("Sentry APM initialized (env=%s)", SENTRY_ENVIRONMENT)
else:
    logger.info("Sentry APM disabled (SENTRY_DSN not set)")

whatsapp = WhatsAppClient()
store = get_store()
calendly = CalendlyClient()

redis_conn = (
    Redis.from_url(
        REDIS_URL,
        socket_timeout=2,
        socket_connect_timeout=2,
        retry_on_timeout=True,
        health_check_interval=30,
    )
    if REDIS_URL
    else None
)
webhook_queue = RQQueue("webhooks", connection=redis_conn) if redis_conn else None

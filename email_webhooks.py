# Backward-compat shim — remove after Phase 8.
from app.email.email_webhooks import *  # noqa: F401, F403
from app.email.email_webhooks import handle_resend_event  # noqa: F401

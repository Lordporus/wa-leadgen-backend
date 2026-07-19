# Backward-compat shim — remove after Phase 8.
from app.email.email_client import *  # noqa: F401, F403
from app.email.email_client import EmailClient, EmailSendError, email_client  # noqa: F401

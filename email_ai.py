# Backward-compat shim — remove after Phase 8.
from app.email.email_ai import *  # noqa: F401, F403
from app.email.email_ai import generate_email_draft  # noqa: F401

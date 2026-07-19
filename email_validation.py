# Backward-compat shim — remove after Phase 8.
from app.email.email_validation import *  # noqa: F401, F403
from app.email.email_validation import validate_lead_email  # noqa: F401

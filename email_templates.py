# Backward-compat shim — remove after Phase 8.
from app.email.email_templates import *  # noqa: F401, F403
from app.email.email_templates import (  # noqa: F401
    apply_merge_fields, build_unsubscribe_url, wrap_email_bodies, is_valid_email_format
)

# Backward-compat shim — remove after Phase 8.
from app.services.rag import *  # noqa: F401, F403
from app.services.rag import retrieve_context  # noqa: F401

# Backward-compat shim — remove after Phase 8.
from app.services.ingestion import *  # noqa: F401, F403
from app.services.ingestion import ingest_document, embed_text, chunk_text  # noqa: F401

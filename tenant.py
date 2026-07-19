# Backward-compat shim — remove after Phase 8.
from app.services.tenant import *  # noqa: F401, F403
from app.services.tenant import (  # noqa: F401
    load_client, get_gemini_for_client, get_pipeline_stages,
    get_won_stage_names, get_lost_stage_names, ClientContext,
    resolve_context_by_phone_id, resolve_context_by_api_key,
    get_all_active_clients, is_configured,
)

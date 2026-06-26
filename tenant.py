"""
Phase 8 — Tenant helper.

Loads the active Client row from Postgres and builds tenant-aware service
instances. Called ONCE at startup in main.py (not on every request).

Per-service deployment model: each Render service has CLIENT_ID set in its
environment variables. There is no per-request routing needed.

Public API
----------
load_client(client_id)          → Client ORM row (or None if Postgres not ready)
get_gemini_for_client(client)   → GeminiClient seeded with client's system_prompt
get_pipeline_stages(client_id)  → ordered list[PipelineStage]
get_won_stage_names(client_id)  → list[str] of stages where is_won=True
get_lost_stage_names(client_id) → list[str] of stages where is_lost=True
"""

import logging
from sqlalchemy import select

from database import SessionLocal, is_configured
from models import Client, PipelineStage

logger = logging.getLogger(__name__)


def load_client(client_id: int) -> "Client | None":
    """
    Fetch the Client row from Postgres by id.

    Returns None (with a warning) if:
    - Postgres is not configured (MIGRATION_MODE=airtable is fine — we just
      won't have per-client prompt / stages from DB, and callers fall back
      to the hardcoded defaults).
    - The client row doesn't exist yet.
    """
    if not is_configured():
        logger.warning(
            f"Postgres not configured — client {client_id} config will use "
            "hardcoded defaults (airtable mode)."
        )
        return None

    try:
        with SessionLocal() as s:
            client = s.execute(
                select(Client).where(Client.id == client_id)
            ).scalar_one_or_none()

            if client is None:
                logger.error(
                    f"Client id={client_id} not found in DB. "
                    "Using hardcoded defaults."
                )
                return None

            # Eagerly load relationships while session is open
            _ = client.leads  # noqa — touch to load
            logger.info(f"Loaded tenant: id={client.id} name={client.name!r}")
            return client

    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to load client {client_id}: {e}")
        return None


def get_gemini_for_client(client: "Client | None") -> "GeminiClient":  # type: ignore[name-defined]
    """
    Return a GeminiClient initialised with this client's system_prompt.
    Falls back to the hardcoded DEFAULT_SYSTEM_PROMPT when:
    - Postgres is not configured (airtable mode)
    - client is None
    - client.system_prompt is blank
    """
    from gemini_client import GeminiClient
    prompt = (client.system_prompt or "").strip() if client else ""
    if prompt:
        logger.info(f"Using per-client system prompt for tenant {client.id}.")
    else:
        logger.info("No per-client prompt found — using default system prompt.")
    return GeminiClient(system_prompt=prompt or None)


def get_pipeline_stages(client_id: int) -> list:
    """
    Return ordered PipelineStage rows for this client.
    Returns an empty list if Postgres is not configured.
    """
    if not is_configured():
        return []
    try:
        with SessionLocal() as s:
            rows = s.execute(
                select(PipelineStage)
                .where(PipelineStage.client_id == client_id)
                .order_by(PipelineStage.position)
            ).scalars().all()
            return list(rows)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to load pipeline stages for client {client_id}: {e}")
        return []


def get_won_stage_names(client_id: int) -> list[str]:
    """
    Return stage names where is_won=True (e.g. ['Booked']).
    Falls back to ['Booked'] if Postgres not available.
    """
    stages = get_pipeline_stages(client_id)
    if not stages:
        return ["Booked"]   # hardcoded fallback — matches Phase 1-7 behaviour
    return [s.name for s in stages if s.is_won]


def get_lost_stage_names(client_id: int) -> list[str]:
    """
    Return stage names where is_lost=True (e.g. ['Lost']).
    Falls back to ['Lost'] if Postgres not available.
    """
    stages = get_pipeline_stages(client_id)
    if not stages:
        return ["Lost"]     # hardcoded fallback — matches Phase 1-7 behaviour
    return [s.name for s in stages if s.is_lost]

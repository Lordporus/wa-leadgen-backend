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

F6 additions (per-request context resolution)
---------------------------------------------
ClientContext                             → dataclass bundling client + gemini + stages
resolve_context_by_phone_id(phone_id)     → ClientContext | None (webhook routing)
resolve_context_by_api_key(raw_api_key)   → ClientContext | None (dashboard auth)
"""

import hashlib
import logging
from dataclasses import dataclass

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


# ── F6: per-request tenant context resolution ─────────────────────────────


@dataclass
class ClientContext:
    """Bundle of everything needed to handle a request for a specific tenant."""
    client: Client
    gemini: object          # GeminiClient (typed as object to avoid circular import)
    won_stages: list[str]
    lost_stages: list[str]


def _build_context(client: Client) -> ClientContext:
    """
    Internal helper: given a detached Client row, build the full
    ClientContext (GeminiClient + pipeline stage names).
    """
    gemini = get_gemini_for_client(client)
    won    = get_won_stage_names(client.id)
    lost   = get_lost_stage_names(client.id)
    return ClientContext(client=client, gemini=gemini, won_stages=won, lost_stages=lost)


def resolve_context_by_phone_id(wa_phone_number_id: str) -> ClientContext | None:
    """
    Resolve full client context from a WhatsApp phone_number_id.

    Used by the webhook handler to route incoming messages to the correct
    tenant. Returns None if Postgres is not configured or no active client
    matches the phone number ID.
    """
    if not is_configured():
        logger.warning(
            "Postgres not configured — cannot resolve client by phone_number_id."
        )
        return None

    try:
        with SessionLocal() as s:
            client = s.execute(
                select(Client).where(
                    Client.wa_phone_number_id == wa_phone_number_id,
                    Client.is_active.is_(True),
                )
            ).scalar_one_or_none()

            if client is None:
                logger.warning(
                    f"No active client found for wa_phone_number_id={wa_phone_number_id}"
                )
                return None

            # Eagerly load relationships while session is open
            _ = client.pipeline_stages  # noqa — touch to load

        logger.info(
            f"Resolved tenant by phone_id: id={client.id} name={client.name!r}"
        )
        return _build_context(client)

    except Exception as e:  # noqa: BLE001
        logger.error(
            f"Failed to resolve client by phone_id {wa_phone_number_id}: {e}"
        )
        return None


def resolve_context_by_api_key(raw_api_key: str) -> ClientContext | None:
    """
    Resolve full client context from a dashboard API key.

    Computes the SHA-256 hex digest of the raw key and looks it up in the
    clients.dashboard_api_key_hash column. Returns None if Postgres is not
    configured or no active client matches the hash.
    """
    if not raw_api_key:
        return None

    if not is_configured():
        logger.warning(
            "Postgres not configured — cannot resolve client by API key hash."
        )
        return None

    key_hash = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()

    try:
        with SessionLocal() as s:
            client = s.execute(
                select(Client).where(
                    Client.dashboard_api_key_hash == key_hash,
                    Client.is_active.is_(True),
                )
            ).scalar_one_or_none()

            if client is None:
                return None

            # Eagerly load relationships while session is open
            _ = client.pipeline_stages  # noqa — touch to load

        logger.info(
            f"Resolved tenant by API key: id={client.id} name={client.name!r}"
        )
        return _build_context(client)

    except Exception as e:  # noqa: BLE001
        logger.error(f"Failed to resolve client by API key hash: {e}")
        return None


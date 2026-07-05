"""
Phase 7 — migration orchestrator.

Three modes, selected by `MIGRATION_MODE` env var:

  airtable (default) → AirtableClient only           (pre-migration, zero risk)
  dual               → DualWriteStore                 (shadow writes to Postgres)
  postgres           → DatabaseClient only            (post-cutover)

`DualWriteStore` writes to BOTH stores but reads from the Airtable side (which
remains source-of-truth during the shadow phase). Postgres write failures are
*logged but never raised*, so a Supabase hiccup cannot break the live pipeline.

All three modes expose the identical public interface used by main.py/scraper.py:
    get_lead, add_lead, update_lead_status, append_message,
    update_lead_info, update_lead_score, _search, table.create()
"""

import logging

from config import MIGRATION_MODE, DATABASE_URL
from airtable_client import AirtableClient

logger = logging.getLogger(__name__)

# Initialise the Postgres engine once at import time (no-op if no DATABASE_URL).
from database import init_engine
init_engine(DATABASE_URL)


class DualWriteStore:
    """
    Writes fan out to Airtable + Postgres. Reads come from Airtable (primary,
    authoritative) so callers see identical data to pre-migration.

    Postgres errors are contained: they never propagate. This guarantees the
    migration cannot degrade the live WhatsApp pipeline.
    """

    def __init__(self, primary: AirtableClient, secondary):
        self._primary = primary
        self._secondary = secondary

    @property
    def table(self):
        # Expose the primary's table shim so scraper.py's `.table.create()` works.
        return self._primary.table

    # ── reads (primary only) ──────────────────────────────────────────────

    def _search(self, formula: str, client_id=None) -> list:
        return self._primary._search(formula, client_id=client_id)

    def get_contacted_leads(self, client_id: int) -> list[dict]:
        return self._primary.get_contacted_leads(client_id)

    def get_lead(self, phone: str) -> dict | None:
        return self._primary.get_lead(phone)

    def get_all_leads(self, client_id=None) -> list:
        return self._primary.get_all_leads(client_id=client_id)

    def get_lead_by_id(self, record_id: str) -> dict | None:
        return self._primary.get_lead_by_id(record_id)

    # ── writes (both; secondary failures contained) ───────────────────────

    def add_lead(self, name: str, phone: str, source: str = "Apify - Google Maps") -> dict | None:
        result = self._primary.add_lead(name, phone, source)
        self._safe(lambda: self._secondary.add_lead(name, phone, source), "add_lead", phone)
        return result

    def update_lead_status(self, phone: str, status: str) -> dict | None:
        result = self._primary.update_lead_status(phone, status)
        self._safe(lambda: self._secondary.update_lead_status(phone, status), "update_lead_status", phone)
        return result

    def update_lead_status_by_id(self, record_id: str, status: str) -> dict | None:
        # Only primary — Postgres secondary doesn't track Airtable record IDs.
        return self._primary.update_lead_status_by_id(record_id, status)

    def append_message(self, phone: str, direction: str, message: str, msg_type: str = "text", wa_message_id: str | None = None) -> bool:
        result = self._primary.append_message(phone, direction, message, msg_type, wa_message_id)
        self._safe(
            lambda: self._secondary.append_message(phone, direction, message, msg_type, wa_message_id),
            "append_message", phone,
        )
        return result

    def update_message_status(self, wa_message_id: str, status: str) -> None:
        # Airtable doesn't support message-level statuses right now. We just proxy to Postgres.
        self._safe(
            lambda: self._secondary.update_message_status(wa_message_id, status),
            "update_message_status", wa_message_id,
        )

    def update_lead_info(self, phone: str, name: str | None, business_name: str | None) -> None:
        self._primary.update_lead_info(phone, name, business_name)
        self._safe(
            lambda: self._secondary.update_lead_info(phone, name, business_name),
            "update_lead_info", phone,
        )

    def update_lead_score(self, phone: str, score: str) -> None:
        self._primary.update_lead_score(phone, score)
        self._safe(lambda: self._secondary.update_lead_score(phone, score), "update_lead_score", phone)

    # ── helper ────────────────────────────────────────────────────────────

    @staticmethod
    def _safe(fn, op: str, phone: str):
        """Run a Postgres write; log and swallow any error."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — intentional: contain migration faults
            logger.error(f"[DualWrite] Postgres {op} failed for {phone}: {e}")


# ── module-level singleton, chosen at import time from MIGRATION_MODE ──────

_store = None

def get_primary_store():
    mode = (MIGRATION_MODE or "airtable").lower()
    if mode in ["postgres", "dual"]:
        from db_client import DatabaseClient
        return DatabaseClient()
    from airtable_client import AirtableClient
    return AirtableClient()

def get_secondary_store():
    mode = (MIGRATION_MODE or "airtable").lower()
    if mode == "dual":
        from airtable_client import AirtableClient
        return AirtableClient()
    return None

def get_store():
    """
    Return the configured lead store (memoised).

    Callers in main.py/scraper.py do `store = get_store()` and use the common
    interface; they never need to know which backend is active.
    """
    global _store
    if _store is not None:
        return _store

    mode = (MIGRATION_MODE or "airtable").lower()

    if mode == "postgres":
        from db_client import DatabaseClient
        _store = DatabaseClient()
        logger.info("Lead store = Postgres (DatabaseClient).")
    elif mode == "dual":
        from db_client import DatabaseClient
        if not DatabaseClient().ok:
            logger.error("MIGRATION_MODE=dual but Postgres not configured — falling back to Airtable.")
            from airtable_client import AirtableClient
            _store = AirtableClient()
        else:
            from airtable_client import AirtableClient
            _store = DualWriteStore(AirtableClient(), DatabaseClient())
            logger.info("Lead store = DualWrite (Airtable primary, Postgres shadow).")
    else:  # "airtable" and any unknown value
        from airtable_client import AirtableClient
        _store = AirtableClient()
        logger.info("Lead store = Airtable.")

    return _store

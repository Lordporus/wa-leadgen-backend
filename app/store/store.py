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
from typing import Protocol

from app.core.config import MIGRATION_MODE, DATABASE_URL
from app.clients.airtable_client import AirtableClient

logger = logging.getLogger(__name__)

# Initialise the Postgres engine once at import time (no-op if no DATABASE_URL).
from app.core.database import init_engine
init_engine(DATABASE_URL)


class LeadStore(Protocol):
    """Tenant-scoped store contract used by WhatsApp routes and jobs."""

    def _search(self, formula: str, client_id: int) -> list: ...
    def get_contacted_leads(self, client_id: int) -> list[dict]: ...
    def get_lead(self, phone: str, client_id: int) -> dict | None: ...
    def get_all_leads(self, client_id: int) -> list: ...
    def get_lead_by_id(self, record_id: str | int, client_id: int) -> dict | None: ...
    def get_messages_for_lead(self, lead_id: str | int, client_id: int) -> list: ...
    def add_lead(
        self,
        name: str,
        phone: str,
        source: str = "Apify - Google Maps",
        *,
        client_id: int,
    ) -> dict | None: ...
    def update_lead_status(
        self,
        phone: str,
        status: str,
        client_id: int,
    ) -> dict | None: ...
    def update_lead_status_by_id(
        self,
        record_id: str | int,
        status: str,
        client_id: int,
    ) -> dict | None: ...
    def update_human_takeover_by_id(
        self,
        record_id: str | int,
        enabled: bool,
        client_id: int,
    ) -> dict | None: ...
    def append_message(
        self,
        phone: str,
        direction: str,
        message: str,
        msg_type: str = "text",
        wa_message_id: str | None = None,
        *,
        client_id: int,
    ) -> bool: ...
    def update_message_status(
        self,
        wa_message_id: str,
        status: str,
        client_id: int,
    ) -> None: ...
    def update_lead_info(
        self,
        phone: str,
        name: str | None,
        business_name: str | None,
        client_id: int,
    ) -> None: ...
    def update_lead_score(self, phone: str, score: str, client_id: int) -> None: ...


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

    def _search(self, formula: str, client_id: int) -> list:
        return self._primary._search(formula, client_id=client_id)

    def get_contacted_leads(self, client_id: int) -> list[dict]:
        return self._primary.get_contacted_leads(client_id)

    def get_lead(self, phone: str, client_id: int) -> dict | None:
        return self._primary.get_lead(phone, client_id=client_id)

    def get_all_leads(self, client_id: int) -> list:
        return self._primary.get_all_leads(client_id=client_id)

    def get_lead_by_id(self, record_id: str | int, client_id: int) -> dict | None:
        return self._primary.get_lead_by_id(str(record_id), client_id=client_id)

    def get_messages_for_lead(self, lead_id: str | int, client_id: int) -> list:
        # Note: In DualWrite mode, reads still come from primary (Airtable), which doesn't support separate messages
        # When MIGRATION_MODE=postgres, this class isn't used, and DatabaseClient is the store.
        if hasattr(self._primary, "get_messages_for_lead"):
            return self._primary.get_messages_for_lead(
                str(lead_id),
                client_id=client_id,
            )
        return []

    # ── writes (both; secondary failures contained) ───────────────────────

    def add_lead(
        self,
        name: str,
        phone: str,
        source: str = "Apify - Google Maps",
        *,
        client_id: int,
    ) -> dict | None:
        if client_id is None:
            return None
        result = self._primary.add_lead(name, phone, source, client_id=client_id)
        self._safe(
            lambda: self._secondary.add_lead(
                name,
                phone,
                source,
                client_id=client_id,
            ),
            "add_lead",
            client_id,
        )
        return result

    def update_lead_status(
        self,
        phone: str,
        status: str,
        client_id: int,
    ) -> dict | None:
        if client_id is None:
            return None
        result = self._primary.update_lead_status(
            phone,
            status,
            client_id=client_id,
        )
        self._safe(
            lambda: self._secondary.update_lead_status(
                phone,
                status,
                client_id=client_id,
            ),
            "update_lead_status",
            client_id,
        )
        return result

    def update_lead_status_by_id(
        self,
        record_id: str | int,
        status: str,
        client_id: int,
    ) -> dict | None:
        if client_id is None:
            return None
        result = self._primary.update_lead_status_by_id(
            str(record_id),
            status,
            client_id=client_id,
        )
        phone = (result or {}).get("fields", {}).get("Phone number type")
        if phone:
            self._safe(
                lambda: self._secondary.update_lead_status(
                    phone,
                    status,
                    client_id=client_id,
                ),
                "update_lead_status_by_id",
                client_id,
            )
        return result

    def update_human_takeover_by_id(
        self,
        record_id: str | int,
        enabled: bool,
        client_id: int,
    ) -> dict | None:
        """Update the read-primary first, then mirror the same scoped lead."""
        if client_id is None:
            return None
        result = self._primary.update_human_takeover_by_id(
            str(record_id),
            enabled,
            client_id=client_id,
        )
        phone = (result or {}).get("fields", {}).get("Phone number type")
        if phone:
            def mirror_to_secondary():
                secondary_record = self._secondary.get_lead(
                    phone,
                    client_id=client_id,
                )
                if secondary_record:
                    self._secondary.update_human_takeover_by_id(
                        secondary_record["id"],
                        enabled,
                        client_id=client_id,
                    )

            self._safe(
                mirror_to_secondary,
                "update_human_takeover_by_id",
                client_id,
            )
        return result

    def append_message(
        self,
        phone: str,
        direction: str,
        message: str,
        msg_type: str = "text",
        wa_message_id: str | None = None,
        *,
        client_id: int,
    ) -> bool:
        if client_id is None:
            return False
        result = self._primary.append_message(
            phone,
            direction,
            message,
            msg_type,
            wa_message_id,
            client_id=client_id,
        )
        self._safe(
            lambda: self._secondary.append_message(
                phone,
                direction,
                message,
                msg_type,
                wa_message_id,
                client_id=client_id,
            ),
            "append_message",
            client_id,
        )
        return result

    def update_message_status(
        self,
        wa_message_id: str,
        status: str,
        client_id: int,
    ) -> None:
        if client_id is None:
            return
        # Airtable doesn't support message-level statuses right now. We just proxy to Postgres.
        self._safe(
            lambda: self._secondary.update_message_status(
                wa_message_id,
                status,
                client_id=client_id,
            ),
            "update_message_status",
            client_id,
        )

    def update_lead_info(
        self,
        phone: str,
        name: str | None,
        business_name: str | None,
        client_id: int,
    ) -> None:
        if client_id is None:
            return
        self._primary.update_lead_info(
            phone,
            name,
            business_name,
            client_id=client_id,
        )
        self._safe(
            lambda: self._secondary.update_lead_info(
                phone,
                name,
                business_name,
                client_id=client_id,
            ),
            "update_lead_info",
            client_id,
        )

    def update_lead_score(
        self,
        phone: str,
        score: str,
        client_id: int,
    ) -> None:
        if client_id is None:
            return
        self._primary.update_lead_score(phone, score, client_id=client_id)
        self._safe(
            lambda: self._secondary.update_lead_score(
                phone,
                score,
                client_id=client_id,
            ),
            "update_lead_score",
            client_id,
        )

    # ── helper ────────────────────────────────────────────────────────────

    @staticmethod
    def _safe(fn, op: str, client_id: int):
        """Run a Postgres write; log and swallow any error."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — intentional: contain migration faults
            logger.error(
                "[DualWrite] Postgres shadow write failed",
                extra={
                    "event": "dual_write_failed",
                    "operation": op,
                    "client_id": client_id,
                    "error_type": type(e).__name__,
                },
            )


# ── module-level singleton, chosen at import time from MIGRATION_MODE ──────

_store: LeadStore | None = None

def get_primary_store():
    mode = (MIGRATION_MODE or "airtable").lower()
    if mode in ["postgres", "dual"]:
        from app.store.db_client import DatabaseClient
        return DatabaseClient()
    from app.clients.airtable_client import AirtableClient
    return AirtableClient()

def get_secondary_store():
    mode = (MIGRATION_MODE or "airtable").lower()
    if mode == "dual":
        from app.clients.airtable_client import AirtableClient
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
        from app.store.db_client import DatabaseClient
        _store = DatabaseClient()
        logger.info("Lead store = Postgres (DatabaseClient).")
    elif mode == "dual":
        from app.store.db_client import DatabaseClient
        if not DatabaseClient().ok:
            logger.error("MIGRATION_MODE=dual but Postgres not configured — falling back to Airtable.")
            from app.clients.airtable_client import AirtableClient
            _store = AirtableClient()
        else:
            from app.clients.airtable_client import AirtableClient
            _store = DualWriteStore(AirtableClient(), DatabaseClient())
            logger.info("Lead store = DualWrite (Airtable primary, Postgres shadow).")
    else:  # "airtable" and any unknown value
        from app.clients.airtable_client import AirtableClient
        _store = AirtableClient()
        logger.info("Lead store = Airtable.")

    return _store

import requests
import logging
from datetime import datetime
from app.core.config import AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

logger = logging.getLogger(__name__)

class AirtableClient:
    """
    Thin wrapper around the Airtable REST API v0.
    Uses raw requests — no pyairtable dependency (avoids pydantic v1/Python 3.12 crash).
    """
    def __init__(self):
        self.ok = all([AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME])
        if not self.ok:
            logger.warning("Airtable credentials not fully configured.")
            return
        self.base_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
        self.headers = {
            "Authorization": f"Bearer {AIRTABLE_API_KEY}",
            "Content-Type": "application/json",
        }
        # Expose a .table stub so scraper.py's direct `airtable.table.create()` call works
        self.table = _TableShim(self)

    # ── internal helpers ──────────────────────────────────────────────────

    def _search(self, formula: str, client_id=None) -> list:
        """Return list of matching Airtable records. client_id ignored — single tenant."""
        if not self.ok: return []
        try:
            resp = requests.get(
                self.base_url,
                headers=self.headers,
                params={"filterByFormula": formula},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("records", [])
        except Exception as e:
            logger.error(f"Airtable search error: {e}")
            return []

    def _create(self, fields: dict) -> dict | None:
        """Create a new record and return it."""
        if not self.ok: return None
        try:
            resp = requests.post(
                self.base_url,
                headers=self.headers,
                json={"fields": fields, "typecast": True},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Airtable create error: {e}")
            return None

    def _update(self, record_id: str, fields: dict) -> dict | None:
        """PATCH-update a record by ID."""
        if not self.ok: return None
        try:
            resp = requests.patch(
                f"{self.base_url}/{record_id}",
                headers=self.headers,
                json={"fields": fields, "typecast": True},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Airtable update error: {e}")
            return None

    # ── public API ────────────────────────────────────────────────────────

    def get_lead(self, phone: str) -> dict | None:
        """Return the first record matching this phone, or None."""
        records = self._search(f"{{Phone number type}}='{phone}'")
        return records[0] if records else None

    def add_lead(self, name: str, phone: str, source: str = "Apify - Google Maps") -> dict | None:
        """Create a new lead record."""
        record = self._create({
            "Name":              name,
            "Phone number type": phone,
            "Source":            source,
            "Status":            "New Lead",
            "Created_At":        datetime.now().isoformat(),
        })
        if record:
            logger.info(f"Added lead: {name} ({phone})")
        return record

    def update_lead_status(self, phone: str, status: str) -> dict | None:
        """Find lead by phone and update its Status field."""
        records = self._search(f"{{Phone number type}}='{phone}'")
        if not records:
            logger.warning(f"Lead not found for status update: {phone}")
            return None
        record_id = records[0]["id"]
        updated = self._update(record_id, {"Status": status})
        if updated:
            logger.info(f"Status updated → {status}: {phone}")
        return updated

    def update_lead_status_by_id(self, record_id: str, status: str) -> dict | None:
        """Update the Status field directly by Airtable record ID."""
        updated = self._update(record_id, {"Status": status})
        if updated:
            logger.info(f"Status updated → {status}: record {record_id}")
        return updated

    def get_all_leads(self, client_id=None) -> list:
        """Return all records from the leads table. client_id ignored — single tenant."""
        return self._search("") if self.ok else []

    def get_contacted_leads(self, client_id: int) -> list[dict]:
        """
        Return contacted leads. Airtable only supports client_id=1.
        Returns empty list for any other client.
        """
        if client_id != 1:
            return []
        return self._search("{Status}='Contacted'")


    def get_lead_by_id(self, record_id: str, client_id: int | None = None) -> dict | None:
        """Return a single record by its Airtable record ID."""
        if not self.ok:
            return None
        try:
            resp = requests.get(
                f"{self.base_url}/{record_id}",
                headers=self.headers,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Airtable get_by_id error: {e}")
            return None

    def get_messages_for_lead(self, lead_id: str, client_id: int | None = None) -> list:
        # Airtable doesn't store separate message rows. Messages are parsed from the Lead's Last_Message field.
        return []

    def append_message(self, phone: str, direction: str, message: str, msg_type: str = "text", wa_message_id: str | None = None) -> bool:
        """Append a message to the Last_Message long text field (used as MVP message log)."""
        records = self._search(f"{{Phone number type}}='{phone}'")
        if not records:
            return True
            
        record = records[0]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {direction.upper()} ({msg_type}): {message}"
        
        current_log = record.get("fields", {}).get("Last_Message", "")
        new_log = f"{current_log}\n{log_entry}" if current_log else log_entry
        self._update(record["id"], {"Last_Message": new_log})
        return True

    def update_message_status(self, wa_message_id: str, status: str) -> None:
        """No-op for Airtable. Messages are stored as a flat text blob,
        so per-message status isn't tracked here."""
        logger.debug(f"AirtableClient ignoring status update {status} for {wa_message_id}")

    def update_lead_info(self, phone: str, name: str | None, business_name: str | None) -> None:
        """Update Name and/or Business_Name fields if values are provided."""
        records = self._search(f"{{Phone number type}}='{phone}'")
        if not records:
            return
        fields = {}
        if name:          fields["Name"]          = name
        if business_name: fields["Business_Name"] = business_name
        if fields:
            self._update(records[0]["id"], fields)
            logger.info(f"Lead info updated for {phone}: {fields}")

    def update_lead_score(self, phone: str, score: str) -> None:
        """Update Lead_Score field."""
        records = self._search(f"{{Phone number type}}='{phone}'")
        if not records:
            return
        updated = self._update(records[0]["id"], {"Lead_Score": score})
        if updated:
            logger.info(f"Lead score updated to {score} for {phone}")

class _TableShim:
    """
    Minimal shim so scraper.py can call `airtable.table.create(fields)` directly
    without any pyairtable dependency.
    """
    def __init__(self, client: AirtableClient):
        self._client = client

    def create(self, fields: dict) -> dict | None:
        return self._client._create(fields)


"""
Phase 7 — Postgres-backed lead store.

`DatabaseClient` is a drop-in replacement for `AirtableClient`. It exposes the
*same* public method signatures and returns records in the *same* shape that
Airtable's REST API returns:

    {"id": <id>, "fields": {Name, "Phone number type", Source, Status,
                            Business_Name, Last_Message, Lead_Score, Created_At}}

This lets `main.py` and `scraper.py` swap the backend by changing one import,
without touching any field-access code. The `.table.create()` shim is kept so
the scraper's existing `airtable.table.create(fields)` call keeps working too.

NOTE on field naming: the Airtable column is literally named "Phone number type"
(with spaces). We preserve that key in `fields` so existing lookups in main.py
(e.g. `r.get("fields", {}).get("Phone number type")`) work unchanged.
"""

import logging
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import SessionLocal, is_configured
from app.core.models import Lead, Message, Client

logger = logging.getLogger(__name__)

# Airtable field aliases (kept identical to keep main.py/scraper.py untouched)
PHONE_KEY = "Phone number type"


class _TableShim:
    """
    Lets `scraper.py` keep calling `store.table.create(fields)` with a raw
    Airtable-style fields dict. Translates the dict into proper lead + message
    writes. Mirrors `airtable_client._TableShim`.
    """

    def __init__(self, client: "DatabaseClient"):
        self._client = client

    def create(self, fields: dict) -> dict | None:
        return self._client._create_from_fields(fields)


class DatabaseClient:
    """Postgres-backed implementation of the lead-store interface."""

    def __init__(self):
        self.ok = is_configured()
        if not self.ok:
            logger.warning("Postgres engine not configured — DatabaseClient will no-op.")
        self.table = _TableShim(self)

    # ── internal helpers ──────────────────────────────────────────────────

    def _session(self):
        if not self.ok:
            return None
        return SessionLocal()

    def _to_fields(self, lead: Lead) -> dict:
        """Convert a Lead ORM object into the Airtable-style `fields` dict."""
        return {
            "Name": lead.name,
            PHONE_KEY: lead.phone,
            "Source": lead.source,
            "Status": lead.status,
            "Business_Name": lead.business_name,
            "Last_Message": lead.last_message,
            "Lead_Score": lead.lead_score,
            "client_id": lead.client_id,
            "is_human_takeover": lead.is_human_takeover,
            "email": lead.email,
            "email_status": lead.email_status,
            "Created_At": lead.created_at.isoformat() if lead.created_at else None,
        }

    def _record(self, lead: Lead) -> dict:
        return {"id": str(lead.id), "fields": self._to_fields(lead)}

    def _create_from_fields(self, fields: dict) -> dict | None:
        """
        Used by the `.table.create()` shim. Accepts an Airtable-style fields
        dict and writes it as a lead (+ optional seed message in Last_Message).
        """
        phone = fields.get(PHONE_KEY)
        if not phone:
            logger.error("Cannot create lead: missing phone in fields.")
            return None

        cid = fields.get("client_id")
        name = fields.get("Name", "WhatsApp User")
        record = self.add_lead(
            name=name,
            phone=phone,
            client_id=cid,
            source=fields.get("Source", "Google Maps - Gurugram"),
        )
        if not record:
            return None

        # If a seed Last_Message blob was passed (scraper context line), persist it
        seed = fields.get("Last_Message")
        if seed:
            self.append_message(phone, direction="system", message=seed, msg_type="system", client_id=cid)

        # Honour explicit Status/Business_Name if provided (scraper sets Business_Name)
        if fields.get("Business_Name") and fields.get("Business_Name") != name:
            self.update_lead_info(phone, name=None, business_name=fields.get("Business_Name"), client_id=cid)
        if fields.get("Status") and fields.get("Status") != "New Lead":
            self.update_lead_status(phone, fields.get("Status"), client_id=cid)

        return record

    # ── public API (mirrors AirtableClient) ───────────────────────────────

    def _search(self, formula: str, client_id: int) -> list:
        """
        Airtable-compat filter. We only support the subset actually used in
        this codebase: `{Status}='<value>'`. Anything else → returns all leads
        (safe fallback for the caller's subsequent in-Python filtering).
        """
        if not self.ok:
            return []
        status_val = _parse_status_formula(formula)
        try:
            with self._session() as s:
                q = select(Lead).where(Lead.client_id == client_id)
                if status_val is not None:
                    q = q.where(Lead.status == status_val)
                rows = s.execute(q).scalars().all()
                return [self._record(r) for r in rows]
        except SQLAlchemyError as e:
            logger.error(f"Postgres search error: {e}")
            return []

    def get_all_leads(self, client_id: int) -> list:
        """Return all leads scoped by client_id."""
        if not self.ok:
            return []
        try:
            with self._session() as s:
                q = select(Lead).where(Lead.client_id == client_id)
                q = q.order_by(Lead.created_at.desc())
                rows = s.execute(q).scalars().all()
                return [self._record(r) for r in rows]
        except SQLAlchemyError as e:
            logger.error(f"Postgres get_all_leads error: {e}")
            return []

    def get_lead(self, phone: str, client_id: int) -> dict | None:
        """Return the first record matching this phone within a tenant, or None."""
        if not self.ok:
            return None
        try:
            with self._session() as s:
                row = s.execute(
                    select(Lead).where(Lead.phone == phone, Lead.client_id == client_id)
                ).scalar_one_or_none()
                return self._record(row) if row else None
        except SQLAlchemyError as e:
            logger.error(f"Postgres get_lead error: {e}")
            return None

    def get_lead_by_id(self, lead_id: int, client_id: int) -> dict | None:
        """Return the first record matching this ID within a tenant, or None."""
        if not self.ok:
            return None
        try:
            with self._session() as s:
                row = s.execute(
                    select(Lead).where(Lead.id == lead_id, Lead.client_id == client_id)
                ).scalar_one_or_none()
                return self._record(row) if row else None
        except SQLAlchemyError as e:
            logger.error(f"Postgres get_lead_by_id error: {e}")
            return None

    def get_messages_for_lead(self, lead_id: int, client_id: int) -> list[dict]:
        """Fetch all messages for a lead, scoping by client_id for tenant isolation."""
        if not self.ok:
            return []
        try:
            with self._session() as s:
                rows = s.execute(
                    select(Message)
                    .join(Lead, Message.lead_id == Lead.id)
                    .where(Message.lead_id == lead_id, Lead.client_id == client_id)
                    .order_by(Message.created_at.asc())
                ).scalars().all()
                return [
                    {
                        "id": r.id,
                        "direction": r.direction,
                        "body": r.body,
                        "msg_type": r.msg_type,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "wa_message_id": r.wa_message_id,
                        "status": r.status,
                    }
                    for r in rows
                ]
        except SQLAlchemyError as e:
            logger.error(f"Postgres get_messages_for_lead error: {e}")
            return []

    def get_contacted_leads(self, client_id: int) -> list[dict]:
        """Return leads with status 'Contacted' for a specific client."""
        if not self.ok:
            return []
        try:
            with self._session() as s:
                rows = s.execute(
                    select(Lead).where(Lead.client_id == client_id, Lead.status == "Contacted")
                ).scalars().all()
                return [self._record(r) for r in rows]
        except SQLAlchemyError as e:
            logger.error(f"Postgres get_contacted_leads error: {e}")
            return []

    def add_lead(self, name: str, phone: str, client_id: int, source: str = "Apify - Google Maps") -> dict | None:
        """Create a new lead record. No-op if the phone already exists for this tenant."""
        if not self.ok:
            return None
        try:
            with self._session() as s:
                existing = s.execute(
                    select(Lead).where(Lead.phone == phone, Lead.client_id == client_id)
                ).scalar_one_or_none()
                if existing:
                    logger.info(f"Lead already exists (no-op): {name} ({phone})")
                    return self._record(existing)

                lead = Lead(name=name, phone=phone, source=source, status="New Lead", client_id=client_id)
                s.add(lead)
                s.commit()
                s.refresh(lead)
                logger.info(f"Added lead: {name} ({phone})")
                return self._record(lead)
        except SQLAlchemyError as e:
            logger.error(f"Postgres add_lead error: {e}")
            return None

    def update_lead_status_by_id(self, record_id: str, status: str, client_id: int) -> dict | None:
        """Update lead status using its primary key, scoped to tenant."""
        if not self.ok:
            return None
        try:
            with self._session() as s:
                row = s.execute(
                    select(Lead).where(Lead.id == int(record_id), Lead.client_id == client_id)
                ).scalar_one_or_none()
                if not row:
                    return None
                row.status = status
                row.updated_at = datetime.utcnow()
                s.commit()
                s.refresh(row)
                logger.info(f"Postgres updated lead {record_id} to {status}")
                return self._record(row)
        except (SQLAlchemyError, ValueError) as e:
            logger.error(f"Postgres update_lead_status_by_id error: {e}")
            return None

    def update_lead_status(self, phone: str, status: str, client_id: int) -> dict | None:
        """Find lead by phone within tenant and update its Status field."""
        if not self.ok:
            return None
        try:
            with self._session() as s:
                row = s.execute(
                    select(Lead).where(Lead.phone == phone, Lead.client_id == client_id)
                ).scalar_one_or_none()
                if not row:
                    logger.warning(f"Lead not found for status update: {phone}")
                    return None
                row.status = status
                row.updated_at = datetime.utcnow()
                s.commit()
                s.refresh(row)
                logger.info(f"Status updated → {status}: {phone}")
                return self._record(row)
        except SQLAlchemyError as e:
            logger.error(f"Postgres update_lead_status error: {e}")
            return None

    def append_message(self, phone: str, direction: str, message: str, msg_type: str = "text", wa_message_id: str | None = None, client_id: int | None = None) -> bool:
        """Append a message row for this lead (tenant-scoped). Returns False on duplicate wamid."""
        if not self.ok:
            return True

        from sqlalchemy.exc import IntegrityError
        try:
            with self._session() as s:
                q = select(Lead).where(Lead.phone == phone)
                if client_id is not None:
                    q = q.where(Lead.client_id == client_id)
                row = s.execute(q).scalar_one_or_none()
                if not row:
                    return True
                s.add(Message(
                    lead_id=row.id,
                    direction=direction.upper(),
                    msg_type=msg_type,
                    body=message,
                    wa_message_id=wa_message_id,
                ))
                row.updated_at = datetime.utcnow()
                s.commit()
                return True
        except IntegrityError:
            return False
        except SQLAlchemyError as e:
            logger.error(f"Postgres append_message error: {e}")
            return True

    def update_lead_info(self, phone: str, name: str | None, business_name: str | None, client_id: int | None = None) -> None:
        """Update Name and/or Business_Name fields if values are provided (tenant-scoped)."""
        if not self.ok:
            return
        try:
            with self._session() as s:
                q = select(Lead).where(Lead.phone == phone)
                if client_id is not None:
                    q = q.where(Lead.client_id == client_id)
                row = s.execute(q).scalar_one_or_none()
                if not row:
                    return
                changed = False
                if name:
                    row.name = name
                    changed = True
                if business_name:
                    row.business_name = business_name
                    changed = True
                if changed:
                    row.updated_at = datetime.utcnow()
                    s.commit()
                    logger.info(f"Lead info updated for {phone}: name={name}, business={business_name}")
        except SQLAlchemyError as e:
            logger.error(f"Postgres update_lead_info error: {e}")

    def update_message_status(self, wa_message_id: str, status: str, client_id: int | None = None) -> None:
        """Update delivery status of a WhatsApp message (tenant-scoped via Lead join)."""
        if not self.ok:
            return
        try:
            with self._session() as s:
                q = select(Message).where(Message.wa_message_id == wa_message_id)
                if client_id is not None:
                    q = q.join(Lead, Message.lead_id == Lead.id).where(Lead.client_id == client_id)
                row = s.execute(q).scalar_one_or_none()
                if row:
                    row.status = status
                    s.commit()
                    logger.info(f"Message {wa_message_id} status updated to {status}")
        except SQLAlchemyError as e:
            logger.error(f"Postgres update_message_status error: {e}")

    def update_lead_score(self, phone: str, score: str, client_id: int | None = None) -> None:
        """Update Lead_Score field (tenant-scoped)."""
        if not self.ok:
            return
        try:
            with self._session() as s:
                q = select(Lead).where(Lead.phone == phone)
                if client_id is not None:
                    q = q.where(Lead.client_id == client_id)
                row = s.execute(q).scalar_one_or_none()
                if not row:
                    return
                row.lead_score = score
                row.updated_at = datetime.utcnow()
                s.commit()
                logger.info(f"Lead score updated to {score} for {phone}")
        except SQLAlchemyError as e:
            logger.error(f"Postgres update_lead_score error: {e}")


def _parse_status_formula(formula: str) -> str | None:
    """
    Extract the status value from an Airtable-style filterByFormula like:
        {Status}='Contacted'
    Returns None if it doesn't match the expected shape.
    """
    if not formula:
        return None
    import re
    m = re.match(r"\{Status\}\s*=\s*'([^']*)'", formula.strip())
    return m.group(1) if m else None

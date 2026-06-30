"""
Phase 7 — one-time Airtable → Postgres backfill.

Pulls every lead from Airtable and writes it (plus its parsed conversation log)
into Postgres. Idempotent: leads already present (by phone) are skipped, so the
script can be re-run safely to catch up.

Usage:
    # 1. Make sure DATABASE_URL is set and the schema is applied:
    #      psql "$DATABASE_URL" -f migrations/001_init.sql
    # 2. Run the backfill:
    python migrate_airtable_to_postgres.py

The script does NOT change Airtable. It only populates Postgres in preparation
for switching MIGRATION_MODE to "dual" (shadow writes).
"""

import re
import logging
from datetime import datetime

from sqlalchemy import select

from airtable_client import AirtableClient
import database
from models import Lead, Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Last_Message log line format (must match airtable_client.append_message) ──
#   [YYYY-MM-DD HH:MM:SS] INBOUND (text): body
LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"(?P<dir>INBOUND|OUTBOUND|SYSTEM)\s+"
    r"\((?P<type>[^)]*)\):\s?(?P<body>.*)$"
)


def parse_last_message(text: str) -> list[dict]:
    """
    Parse an Airtable `Last_Message` blob into individual message dicts:
        {created_at: datetime, direction: str, msg_type: str, body: str}
    Unparseable lines are skipped (logged at debug).
    """
    messages = []
    if not text:
        return messages
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = LOG_LINE_RE.match(line)
        if not m:
            logger.debug(f"Skipping unparseable log line: {line!r}")
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        messages.append({
            "created_at": ts,
            "direction": m.group("dir"),
            "msg_type": m.group("type"),
            "body": m.group("body"),
        })
    return messages


def fetch_all_airtable_leads(airtable: AirtableClient) -> list[dict]:
    """Return ALL Airtable records (ignores the formula — pulls everything)."""
    return airtable._search("")  # empty formula → no filter in Airtable semantics


def backfill():
    from config import DATABASE_URL
    from database import init_engine
    init_engine(DATABASE_URL)
    if not database.is_configured():
        logger.error("DATABASE_URL not configured. Aborting backfill.")
        return

    airtable = AirtableClient()
    if not airtable.ok:
        logger.error("Airtable not configured. Aborting backfill.")
        return

    records = fetch_all_airtable_leads(airtable)
    logger.info(f"Airtable returned {len(records)} lead(s).")

    inserted = 0
    skipped = 0
    msg_inserted = 0
    msg_unparsed = 0

    with database.SessionLocal() as s:
        for r in records:
            f = r.get("fields", {})
            phone = f.get("Phone number type")
            if not phone:
                logger.warning(f"Record {r.get('id')} has no phone — skipping.")
                continue

            existing = s.execute(select(Lead).where(Lead.phone == phone)).scalar_one_or_none()
            if existing:
                skipped += 1
                continue

            created_at_raw = f.get("Created_At")
            try:
                created_at = datetime.fromisoformat(created_at_raw) if created_at_raw else datetime.utcnow()
            except ValueError:
                created_at = datetime.utcnow()

            lead = Lead(
                phone=phone,
                name=f.get("Name") or "WhatsApp User",
                source=f.get("Source"),
                status=f.get("Status") or "New Lead",
                business_name=f.get("Business_Name"),
                lead_score=f.get("Lead_Score"),
                client_id=1,  # default tenant
                created_at=created_at,
            )
            s.add(lead)
            s.flush()  # populate lead.id

            msgs = parse_last_message(f.get("Last_Message", ""))
            for m in msgs:
                s.add(Message(
                    lead_id=lead.id,
                    direction=m["direction"],
                    msg_type=m["msg_type"],
                    body=m["body"],
                    created_at=m["created_at"],
                ))
                msg_inserted += 1

            if f.get("Last_Message") and not msgs:
                msg_unparsed += 1
                logger.warning(f"Last_Message present but no lines parsed for {phone}")

            inserted += 1

        s.commit()

    # ── reconciliation report ──────────────────────────────────────────────
    with database.SessionLocal() as s:
        pg_lead_count = s.execute(select(Lead)).scalars().all()
        pg_msg_count = s.execute(select(Message)).scalars().all()

    logger.info(
        "\n" + "=" * 60 + "\n"
        "  BACKFILL COMPLETE\n"
        f"  Airtable leads fetched : {len(records)}\n"
        f"  Postgres leads inserted: {inserted} (skipped {skipped} dupes)\n"
        f"  Messages parsed        : {msg_inserted}\n"
        f"  Records w/ unparsable log: {msg_unparsed}\n"
        f"  Postgres totals now    : {len(pg_lead_count)} leads, {len(pg_msg_count)} messages\n"
        + "=" * 60
    )

    # Sanity check: counts must match
    if len(records) != inserted + skipped:
        logger.error(
            f"RECONCILIATION MISMATCH: fetched {len(records)} but inserted+skipped={inserted + skipped}. Investigate."
        )
    else:
        logger.info("Reconciliation OK: fetched == inserted + skipped.")


if __name__ == "__main__":
    backfill()

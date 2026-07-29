"""Staging-only, idempotent Phase 4 Airtable-to-Postgres backfill."""
from __future__ import annotations

import argparse
import os
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import selectinload

from app.clients.airtable_client import AirtableClient
from app.core import database
from app.core.config import DATABASE_URL
from app.core.models import Lead, Message
from app.services.reconciliation import (
    classify_airtable_record,
    eligible_for_backfill,
    message_fingerprint,
    normalize_phone,
    parse_airtable_messages,
)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _message_key(timestamp: datetime | str | None, direction: str, kind: str, body: str) -> tuple[str, str, str, str]:
    if isinstance(timestamp, datetime):
        timestamp = timestamp.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    return (str(timestamp or ""), direction.upper(), kind, body)


def _database_identity(database_url: str) -> str:
    """Return a non-secret identity used to reject the production database."""
    try:
        url = make_url(database_url)
    except Exception as error:  # noqa: BLE001 - reject malformed write targets
        raise RuntimeError("DATABASE_URL is invalid; refusing backfill write") from error
    if not url.host or not url.database:
        raise RuntimeError("DATABASE_URL must include host and database; refusing backfill write")
    port = url.port or 5432
    return f"{url.host.lower()}:{port}/{url.database}"


def _validate_apply_target(database_url: str | None) -> None:
    """Require an explicit staging acknowledgement and reject the production DB."""
    if os.getenv("APP_ENV", "").strip().lower() != "staging":
        raise RuntimeError("--apply is restricted to APP_ENV=staging")
    if os.getenv("BACKFILL_APPLY_CONFIRMATION", "") != "staging-only":
        raise RuntimeError("--apply requires BACKFILL_APPLY_CONFIRMATION=staging-only")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for --apply")
    production_identity = os.getenv("PRODUCTION_DATABASE_IDENTITY", "").strip().lower()
    if not production_identity:
        raise RuntimeError("--apply requires PRODUCTION_DATABASE_IDENTITY to protect production")
    if _database_identity(database_url).lower() == production_identity:
        raise RuntimeError("--apply refused: DATABASE_URL identifies the production database")


def backfill(client_id: int, *, apply: bool = False, approved_test_record_ids: set[str] | None = None) -> dict[str, int]:
    """Plan by default; writes only to explicitly marked staging environments."""
    if apply:
        _validate_apply_target(DATABASE_URL)
    database.init_engine(DATABASE_URL)
    airtable = AirtableClient()
    if not airtable.ok or airtable.client_id != client_id:
        raise RuntimeError("configured Airtable adapter cannot read the requested tenant")
    if not database.is_configured() or database.SessionLocal is None:
        raise RuntimeError("Postgres is not configured")

    result = {
        "leads_to_insert": 0,
        "leads_existing": 0,
        "messages_to_insert": 0,
        "stage_repairs": 0,
        "takeover_repairs": 0,
        "created_timestamp_repairs": 0,
        "test_records_skipped": 0,
    }
    records = airtable.get_all_leads(client_id=client_id)
    with database.SessionLocal() as session:
        real_phones = set(session.execute(select(Lead.phone).where(Lead.client_id == client_id)).scalars())
        for record in records:
            fields = record.get("fields", {})
            phone = normalize_phone(fields.get("Phone number type"))
            classification, _ = classify_airtable_record(record, {normalize_phone(value) for value in real_phones})
            if not eligible_for_backfill(record.get("id"), classification, approved_test_record_ids):
                result["test_records_skipped"] += 1
                continue
            if not phone:
                continue
            lead = session.execute(
                select(Lead).options(selectinload(Lead.messages)).where(
                    Lead.client_id == client_id, Lead.phone == phone
                )
            ).scalar_one_or_none()
            if lead is None:
                result["leads_to_insert"] += 1
                if not apply:
                    continue
                lead = Lead(
                    client_id=client_id,
                    phone=phone,
                    name=fields.get("Name") or "WhatsApp User",
                    source=fields.get("Source"),
                    status=fields.get("Status") or "New Lead",
                    business_name=fields.get("Business_Name"),
                    lead_score=fields.get("Lead_Score"),
                    is_human_takeover=bool(fields.get("is_human_takeover", False)),
                    created_at=_parse_timestamp(fields.get("Created_At")) or datetime.utcnow(),
                )
                session.add(lead)
                session.flush()
            else:
                result["leads_existing"] += 1
                # Airtable remains canonical in dual mode. These changes are
                # staging-only repair candidates; production uses dry-run.
                source_status = fields.get("Status") or "New Lead"
                source_takeover = bool(fields.get("is_human_takeover", False))
                source_created_at = _parse_timestamp(fields.get("Created_At"))
                if lead.status != source_status:
                    result["stage_repairs"] += 1
                    if apply:
                        lead.status = source_status
                if bool(lead.is_human_takeover) != source_takeover:
                    result["takeover_repairs"] += 1
                    if apply:
                        lead.is_human_takeover = source_takeover
                if source_created_at and _message_key(lead.created_at, "", "", "") != _message_key(source_created_at, "", "", ""):
                    result["created_timestamp_repairs"] += 1
                    if apply:
                        lead.created_at = source_created_at

            existing = {
                message_fingerprint({
                    "timestamp": message.created_at,
                    "direction": message.direction,
                    "kind": message.msg_type,
                    "body": message.body or "",
                })
                for message in lead.messages
            }
            for item in parse_airtable_messages(fields.get("Last_Message")):
                key = message_fingerprint(item)
                if key in existing:
                    continue
                result["messages_to_insert"] += 1
                if apply:
                    session.add(Message(
                        lead_id=lead.id,
                        direction=item["direction"].upper(),
                        msg_type=item["kind"],
                        body=item["body"],
                        created_at=_parse_timestamp(item["timestamp"]) or datetime.utcnow(),
                    ))
                existing.add(key)
        if apply:
            session.commit()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage-only idempotent Airtable backfill")
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-test-record-id", action="append", default=[])
    args = parser.parse_args()
    print(backfill(args.client_id, apply=args.apply, approved_test_record_ids=set(args.approved_test_record_id)))

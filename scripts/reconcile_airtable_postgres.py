"""Read-only Phase 4 reconciliation report. Never writes either source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.clients.airtable_client import AirtableClient
from app.core import database
from app.core.config import DATABASE_URL
from app.core.models import Lead
from app.services.reconciliation import (
    airtable_snapshot,
    classify_airtable_record,
    normalize_phone,
    postgres_snapshot,
    reconcile,
)


def run(client_id: int, *, trust_postgres_preservation_set: bool = False) -> dict:
    database.init_engine(DATABASE_URL)
    airtable = AirtableClient()
    if not airtable.ok or airtable.client_id != client_id:
        raise RuntimeError("configured Airtable adapter cannot read the requested tenant")
    if not database.is_configured() or database.SessionLocal is None:
        raise RuntimeError("Postgres is not configured")
    records = airtable.get_all_leads(client_id=client_id)
    with database.SessionLocal() as session:
        leads = session.execute(select(Lead).options(selectinload(Lead.messages)).where(Lead.client_id == client_id)).scalars().all()
    postgres = [postgres_snapshot(lead) for lead in leads]
    real_phones = {normalize_phone(lead.phone) for lead in leads}
    airtable = []
    for record in records:
        classification, reason = classify_airtable_record(
            record,
            real_phones,
            trust_postgres_preservation_set=trust_postgres_preservation_set,
        )
        airtable.append(airtable_snapshot(record, client_id, classification=classification, classification_reason=reason))
    report = reconcile(airtable, postgres)
    report["classification_mode"] = "trusted_postgres_preservation_set" if trust_postgres_preservation_set else "strict"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only Airtable/Postgres reconciliation")
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--trust-postgres-preservation-set", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.client_id, trust_postgres_preservation_set=args.trust_postgres_preservation_set)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)

"""Phase 1 production-safe Airtable-to-Postgres lead backfill.

Dry-run by default. Production writes require a verified pre-mutation export,
an explicit approval ID, and a non-destructive confirmation phrase.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.engine import make_url

from app.clients.airtable_client import AirtableClient
from app.core import database
from app.core.config import DATABASE_URL
from app.core.models import Lead, PipelineStage
from app.services.reconciliation import normalize_phone


PRODUCTION_CONFIRMATION = "production-phase1-non-destructive"
DEFAULT_STAGE_FLAGS = {
    "New Lead": (False, False),
    "Contacted": (False, False),
    "Qualified": (False, False),
    "Booked": (True, False),
    "Lost": (False, True),
}
_TEST_MARKER_RE = re.compile(r"\b(test|dummy|sample)\b|meta test", re.IGNORECASE)
_COPY_FIELDS = (
    "name",
    "source",
    "status",
    "business_name",
    "lead_score",
    "is_human_takeover",
    "created_at",
)


def _database_identity(database_url: str) -> str:
    try:
        url = make_url(database_url)
    except Exception as error:  # noqa: BLE001
        raise RuntimeError("DATABASE_URL is invalid") from error
    if not url.host or not url.database:
        raise RuntimeError("DATABASE_URL must include host and database")
    return f"{url.host.lower()}:{url.port or 5432}/{url.database}"


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _timestamp_key(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.replace(tzinfo=None, microsecond=0).isoformat()


def _json_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_value) + "\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: Any) -> str:
    rendered = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_value,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _postgres_export_payload(client_id: int, leads: list[Lead]) -> dict[str, Any]:
    session_local = database.SessionLocal
    if session_local is None:
        raise RuntimeError("Postgres is not configured")
    with session_local() as session:
        stages = session.execute(
            select(PipelineStage)
            .where(PipelineStage.client_id == client_id)
            .order_by(PipelineStage.position)
        ).scalars().all()
    return {
        "leads": [
            {
                column.name: _json_value(getattr(lead, column.name))
                for column in Lead.__table__.columns
            }
            for lead in leads
        ],
        "pipeline_stages": [
            {
                column.name: _json_value(getattr(stage, column.name))
                for column in PipelineStage.__table__.columns
            }
            for stage in stages
        ],
    }


def _mask_record_id(record_id: object) -> str:
    value = str(record_id or "")
    return value if len(value) <= 10 else f"{value[:6]}…{value[-4:]}"


def _source_values(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": fields.get("Name") or "WhatsApp User",
        "source": fields.get("Source"),
        "status": fields.get("Status") or "New Lead",
        "business_name": fields.get("Business_Name"),
        "lead_score": fields.get("Lead_Score"),
        "is_human_takeover": bool(fields.get("is_human_takeover", False)),
        "created_at": _parse_timestamp(fields.get("Created_At")),
    }


def _conflicts(lead: Lead, source: dict[str, Any]) -> list[str]:
    conflicts: list[str] = []
    for field in _COPY_FIELDS:
        source_value = source[field]
        if field in {"source", "business_name", "lead_score"} and source_value in (None, ""):
            continue
        current = getattr(lead, field)
        if field == "created_at":
            if source_value and _timestamp_key(current) != _timestamp_key(source_value):
                conflicts.append(field)
        elif current != source_value:
            conflicts.append(field)
    return conflicts


def _suspicious_reasons(
    record: dict[str, Any],
    duplicate_phones: set[str],
) -> list[str]:
    fields = record.get("fields", {})
    phone = normalize_phone(fields.get("Phone number type"))
    reasons: list[str] = []
    if not phone:
        reasons.append("missing_phone")
    if phone in duplicate_phones:
        reasons.append("duplicate_normalized_phone")
    marker_text = f"{fields.get('Name', '')} {fields.get('Source', '')}"
    if _TEST_MARKER_RE.search(marker_text):
        reasons.append("test_marker")
    if phone.startswith("1555"):
        reasons.append("meta_test_range")
    return reasons


def build_preview(
    records: list[dict[str, Any]],
    leads: list[Lead],
    approved_record_ids: set[str] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Build a PII-safe preview and exact approved Airtable record-ID set."""
    explicit_approvals = approved_record_ids or set()
    phone_counts = Counter(
        phone
        for record in records
        if (phone := normalize_phone(record.get("fields", {}).get("Phone number type")))
    )
    duplicate_phones = {phone for phone, count in phone_counts.items() if count > 1}
    approved_duplicate_counts = Counter(
        normalize_phone(record.get("fields", {}).get("Phone number type"))
        for record in records
        if str(record.get("id") or "") in explicit_approvals
        and normalize_phone(record.get("fields", {}).get("Phone number type"))
        in duplicate_phones
    )
    if any(count > 1 for count in approved_duplicate_counts.values()):
        raise RuntimeError(
            "approve at most one Airtable record per duplicate phone group"
        )
    existing = {normalize_phone(lead.phone): lead for lead in leads}
    approved_ids: set[str] = set()
    preview: dict[str, Any] = {
        "counts": {
            "airtable": len(records),
            "postgres": len(leads),
            "to_insert": 0,
            "already_matching": 0,
            "conflicts": 0,
            "suspicious": 0,
        },
        "records_to_insert": [],
        "records_already_matching": [],
        "records_with_conflicts": [],
        "suspicious_records": [],
    }

    for record in records:
        record_id = str(record.get("id") or "")
        fields = record.get("fields", {})
        phone = normalize_phone(fields.get("Phone number type"))
        reasons = _suspicious_reasons(record, duplicate_phones)
        if "missing_phone" in reasons or (
            reasons and record_id not in explicit_approvals
        ):
            preview["counts"]["suspicious"] += 1
            preview["suspicious_records"].append(
                {
                    "airtable_id": _mask_record_id(record_id),
                    "stage": fields.get("Status") or "<blank>",
                    "reasons": reasons,
                }
            )
            continue
        if not phone:
            continue

        approved_ids.add(record_id)
        lead = existing.get(phone)
        if lead is None:
            preview["counts"]["to_insert"] += 1
            preview["records_to_insert"].append(
                {
                    "airtable_id": _mask_record_id(record_id),
                    "stage": fields.get("Status") or "New Lead",
                }
            )
            continue

        conflict_fields = _conflicts(lead, _source_values(fields))
        if conflict_fields:
            item: dict[str, Any] = {
                "airtable_id": _mask_record_id(record_id),
                "postgres_id": lead.id,
                "fields": conflict_fields,
            }
            if "status" in conflict_fields:
                item["airtable_stage"] = fields.get("Status") or "New Lead"
                item["postgres_stage"] = lead.status
            preview["counts"]["conflicts"] += 1
            preview["records_with_conflicts"].append(item)
        else:
            preview["counts"]["already_matching"] += 1
            preview["records_already_matching"].append(
                {
                    "airtable_id": _mask_record_id(record_id),
                    "postgres_id": lead.id,
                }
            )
    return preview, approved_ids


def _load_sources(client_id: int) -> tuple[list[dict[str, Any]], list[Lead]]:
    database.init_engine(DATABASE_URL)
    airtable = AirtableClient()
    if not airtable.ok or airtable.client_id != client_id:
        raise RuntimeError("configured Airtable adapter cannot read the requested tenant")
    if not database.is_configured() or database.SessionLocal is None:
        raise RuntimeError("Postgres is not configured")
    records = airtable.get_all_leads(client_id=client_id)
    with database.SessionLocal() as session:
        leads = session.execute(
            select(Lead).where(Lead.client_id == client_id).order_by(Lead.id)
        ).scalars().all()
    return records, list(leads)


def export_state(client_id: int, output_dir: Path) -> Path:
    """Export full Airtable/Postgres lead state before any production mutation."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    records, leads = _load_sources(client_id)
    if output_dir.exists():
        raise RuntimeError("export directory already exists")
    output_dir.mkdir(parents=True)

    postgres_payload = _postgres_export_payload(client_id, leads)

    airtable_path = output_dir / "airtable-leads.json"
    postgres_path = output_dir / "postgres-leads.json"
    _write_private_json(airtable_path, records)
    _write_private_json(postgres_path, postgres_payload)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "client_id": client_id,
        "database_identity": _database_identity(DATABASE_URL),
        "counts": {"airtable": len(records), "postgres": len(leads)},
        "files": [
            {"name": airtable_path.name, "sha256": _sha256_file(airtable_path)},
            {"name": postgres_path.name, "sha256": _sha256_file(postgres_path)},
        ],
    }
    manifest_path = output_dir / "manifest.json"
    _write_private_json(manifest_path, manifest)
    return manifest_path


def verify_backup_manifest(path: Path, client_id: int) -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        created_at = datetime.fromisoformat(manifest["created_at"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("production apply requires a valid backup manifest") from error
    if manifest.get("client_id") != client_id:
        raise RuntimeError("backup client_id does not match")
    if manifest.get("database_identity") != _database_identity(DATABASE_URL):
        raise RuntimeError("backup database identity does not match DATABASE_URL")
    if datetime.now(timezone.utc) - created_at > timedelta(hours=24):
        raise RuntimeError("backup manifest is older than 24 hours")
    files = manifest.get("files", [])
    if len(files) != 2:
        raise RuntimeError("backup manifest must contain Airtable and Postgres exports")
    for item in files:
        backup_path = path.parent / item["name"]
        if not backup_path.is_file() or _sha256_file(backup_path) != item["sha256"]:
            raise RuntimeError(f"backup verification failed for {item['name']}")


def assert_live_sources_match_backup(
    backup_manifest: Path,
    client_id: int,
    records: list[dict[str, Any]],
    leads: list[Lead],
) -> None:
    airtable_backup = json.loads(
        (backup_manifest.parent / "airtable-leads.json").read_text(encoding="utf-8")
    )
    postgres_backup = json.loads(
        (backup_manifest.parent / "postgres-leads.json").read_text(encoding="utf-8")
    )
    current_airtable = sorted(records, key=lambda item: str(item.get("id") or ""))
    saved_airtable = sorted(
        airtable_backup,
        key=lambda item: str(item.get("id") or ""),
    )
    current_postgres = _postgres_export_payload(client_id, leads)
    if _canonical_hash(current_airtable) != _canonical_hash(saved_airtable):
        raise RuntimeError("Airtable changed after backup; create a new export")
    if _canonical_hash(current_postgres) != _canonical_hash(postgres_backup):
        raise RuntimeError("Postgres changed after backup; create a new export")


def validate_production_apply(backup_manifest: Path | None, client_id: int) -> None:
    if os.getenv("APP_ENV", "").strip().lower() != "production":
        raise RuntimeError("--apply requires APP_ENV=production")
    if os.getenv("BACKFILL_APPLY_CONFIRMATION") != PRODUCTION_CONFIRMATION:
        raise RuntimeError(
            f"--apply requires BACKFILL_APPLY_CONFIRMATION={PRODUCTION_CONFIRMATION}"
        )
    if not os.getenv("BACKFILL_APPROVAL_ID", "").strip():
        raise RuntimeError("--apply requires BACKFILL_APPROVAL_ID")
    if backup_manifest is None:
        raise RuntimeError("--apply requires --backup-manifest")
    verify_backup_manifest(backup_manifest, client_id)


def apply_backfill(
    client_id: int,
    records: list[dict[str, Any]],
    approved_ids: set[str],
    *,
    repair_default_stage_flags: bool,
) -> dict[str, int]:
    """Apply insert/update-only repairs in one transaction."""
    result = {
        "inserted": 0,
        "existing": 0,
        "field_repairs": 0,
        "pipeline_stage_repairs": 0,
    }
    session_local = database.SessionLocal
    if session_local is None:
        raise RuntimeError("Postgres is not configured")
    with session_local() as session:
        for record in records:
            if str(record.get("id") or "") not in approved_ids:
                continue
            fields = record.get("fields", {})
            phone = normalize_phone(fields.get("Phone number type"))
            if not phone:
                continue
            source = _source_values(fields)
            values = {
                "client_id": client_id,
                "phone": phone,
                "name": source["name"],
                "source": source["source"],
                "status": source["status"],
                "business_name": source["business_name"],
                "lead_score": source["lead_score"],
                "is_human_takeover": source["is_human_takeover"],
                "created_at": source["created_at"] or datetime.now(timezone.utc),
            }
            inserted_id = session.execute(
                postgres_insert(Lead)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_leads_client_phone")
                .returning(Lead.id)
            ).scalar_one_or_none()
            lead = session.execute(
                select(Lead).where(Lead.client_id == client_id, Lead.phone == phone)
            ).scalar_one()
            if inserted_id is not None:
                result["inserted"] += 1
                continue

            result["existing"] += 1
            conflict_fields = _conflicts(lead, source)
            for field in conflict_fields:
                setattr(lead, field, source[field])
            result["field_repairs"] += len(conflict_fields)

        if repair_default_stage_flags:
            stages = session.execute(
                select(PipelineStage).where(PipelineStage.client_id == client_id)
            ).scalars().all()
            for stage in stages:
                expected = DEFAULT_STAGE_FLAGS.get(stage.name)
                if expected is None:
                    continue
                if (bool(stage.is_won), bool(stage.is_lost)) != expected:
                    stage.is_won, stage.is_lost = expected
                    result["pipeline_stage_repairs"] += 1

        session.commit()
        result["postgres_after"] = int(
            session.execute(
                select(func.count()).select_from(Lead).where(Lead.client_id == client_id)
            ).scalar_one()
        )
    return result


def run(
    client_id: int,
    *,
    apply: bool = False,
    backup_manifest: Path | None = None,
    preview_output: Path | None = None,
    approved_record_ids: set[str] | None = None,
    repair_default_stage_flags: bool = False,
) -> dict[str, Any]:
    records, leads = _load_sources(client_id)
    preview, approved_ids = build_preview(records, leads, approved_record_ids)
    if preview_output:
        _write_private_json(preview_output, preview)
    result: dict[str, Any] = {"preview": preview["counts"], "apply": None}
    if apply:
        validate_production_apply(backup_manifest, client_id)
        assert backup_manifest is not None
        assert_live_sources_match_backup(backup_manifest, client_id, records, leads)
        result["apply"] = apply_backfill(
            client_id,
            records,
            approved_ids,
            repair_default_stage_flags=repair_default_stage_flags,
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Phase 1 production-safe Airtable-to-Postgres backfill"
    )
    parser.add_argument("--client-id", type=int, required=True)
    parser.add_argument("--export-dir", type=Path)
    parser.add_argument("--preview-output", type=Path)
    parser.add_argument("--backup-manifest", type=Path)
    parser.add_argument("--approved-record-id", action="append", default=[])
    parser.add_argument("--repair-default-stage-flags", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    manifest = export_state(args.client_id, args.export_dir) if args.export_dir else None
    result = run(
        args.client_id,
        apply=args.apply,
        backup_manifest=args.backup_manifest,
        preview_output=args.preview_output,
        approved_record_ids=set(args.approved_record_id),
        repair_default_stage_flags=args.repair_default_stage_flags,
    )
    print(
        json.dumps(
            {
                "backup_manifest": str(manifest) if manifest else None,
                "result": result,
            },
            sort_keys=True,
        )
    )





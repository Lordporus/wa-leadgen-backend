"""Pure Phase 4 reconciliation primitives for Airtable-read-primary migration."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_MESSAGE_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\]\s+(?P<direction>\w+)\s+\((?P<kind>[^)]*)\):\s?(?P<body>.*)$"
)


def normalize_phone(value: object) -> str:
    """Canonical comparison key; deliberately never guesses a country code."""
    return re.sub(r"\D", "", str(value or ""))


def parse_airtable_messages(last_message: object) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for line in str(last_message or "").splitlines():
        match = _MESSAGE_RE.match(line.strip())
        if not match:
            continue
        messages.append({key: value.strip() for key, value in match.groupdict().items()})
    return messages


def normalize_timestamp(value: object) -> str:
    """Compare legacy naive and Postgres timezone timestamps at second precision."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw[:19]


def message_fingerprint(message: dict[str, Any]) -> str:
    # Airtable legacy blobs have no provider IDs and record naive seconds.
    # Provider IDs remain an audit field, not part of cross-store equivalence.
    value = "|".join((
        normalize_timestamp(message.get("timestamp")),
        str(message.get("direction", "")).strip().lower(),
        str(message.get("kind", "")).strip().lower(),
        str(message.get("body", "")).strip(),
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LeadSnapshot:
    client_id: int
    normalized_phone: str
    status: str
    is_human_takeover: bool
    created_at: str | None
    updated_at: str | None
    messages: tuple[str, ...]
    classification: str = "real_production"
    classification_reason: str = "postgres_preservation_set"


def classify_airtable_record(
    record: dict[str, Any],
    real_production_phones: set[str],
    *,
    trust_postgres_preservation_set: bool = False,
) -> tuple[str, str]:
    """Classify without treating an unmatched record as test data by default."""
    phone = normalize_phone(record.get("fields", {}).get("Phone number type"))
    if phone and phone in real_production_phones:
        return "real_production", "matches_preserved_postgres_phone"
    if trust_postgres_preservation_set:
        return "known_development_test", "operator_attested_airtable_only_development_data"
    return "ambiguous_unclassified", "does_not_match_preserved_postgres_phone"


def eligible_for_backfill(record_id: str | None, classification: str, approved_test_record_ids: set[str] | None = None) -> bool:
    """Test/development rows require an explicit record-ID approval to copy."""
    return classification == "real_production" or bool(record_id and record_id in (approved_test_record_ids or set()))


def airtable_snapshot(
    record: dict[str, Any],
    client_id: int,
    *,
    classification: str = "ambiguous_unclassified",
    classification_reason: str = "not_classified",
) -> LeadSnapshot:
    fields = record.get("fields", {})
    messages = tuple(sorted(message_fingerprint(message) for message in parse_airtable_messages(fields.get("Last_Message"))))
    return LeadSnapshot(
        client_id=client_id,
        normalized_phone=normalize_phone(fields.get("Phone number type")),
        status=str(fields.get("Status") or "New Lead"),
        is_human_takeover=bool(fields.get("is_human_takeover", False)),
        created_at=normalize_timestamp(fields.get("Created_At")) or None,
        updated_at=normalize_timestamp(fields.get("Updated_At") or fields.get("Created_At")) or None,
        messages=messages,
        classification=classification,
        classification_reason=classification_reason,
    )


def postgres_snapshot(lead: Any) -> LeadSnapshot:
    messages = tuple(sorted(message_fingerprint({
        "timestamp": getattr(message, "created_at", None),
        "direction": getattr(message, "direction", ""),
        "kind": getattr(message, "msg_type", ""),
        "body": getattr(message, "body", ""),
        "provider_id": getattr(message, "wa_message_id", None) or getattr(message, "provider_message_id", None),
    }) for message in getattr(lead, "messages", [])))
    return LeadSnapshot(
        client_id=int(lead.client_id),
        normalized_phone=normalize_phone(lead.phone),
        status=str(lead.status or "New Lead"),
        is_human_takeover=bool(lead.is_human_takeover),
        created_at=normalize_timestamp(lead.created_at) or None,
        updated_at=normalize_timestamp(lead.updated_at) or None,
        messages=messages,
        classification="real_production",
        classification_reason="preserved_postgres_record",
    )


def reconcile(airtable: list[LeadSnapshot], postgres: list[LeadSnapshot]) -> dict[str, Any]:
    """Compare tenant-owned snapshots. Missing ownership/phone are high severity."""
    test_rows = [row for row in airtable if row.classification == "known_development_test"]
    eligible_airtable = [row for row in airtable if row.classification != "known_development_test"]
    report: dict[str, Any] = {
        "counts": {"airtable": len(airtable), "postgres": len(postgres), "test_excluded": len(test_rows)},
        "classification": dict(Counter(row.classification for row in airtable)),
        "findings": [],
        "test_data_noise": [],
    }
    for row in test_rows:
        if not row.normalized_phone:
            report["test_data_noise"].append({"kind": "missing_phone", "classification_reason": row.classification_reason})
    for side, rows in (("airtable", eligible_airtable), ("postgres", postgres)):
        duplicates = [key for key, count in Counter((row.client_id, row.normalized_phone) for row in rows).items() if count > 1]
        for client_id, phone in duplicates:
            report["findings"].append({"severity": "high", "kind": "duplicate_normalized_phone", "side": side, "client_id": client_id, "phone": phone})
        for row in rows:
            if not row.normalized_phone:
                report["findings"].append({"severity": "high", "kind": "missing_phone", "side": side, "client_id": row.client_id})

    pg_by_key = {(row.client_id, row.normalized_phone): row for row in postgres}
    at_by_key = {(row.client_id, row.normalized_phone): row for row in eligible_airtable}
    for key in sorted(set(at_by_key) | set(pg_by_key)):
        at_row, pg_row = at_by_key.get(key), pg_by_key.get(key)
        if at_row is None or pg_row is None:
            report["findings"].append({"severity": "high", "kind": "missing_lead", "client_id": key[0], "phone": key[1], "missing_from": "airtable" if at_row is None else "postgres"})
            continue
        for field in ("status", "is_human_takeover"):
            if getattr(at_row, field) != getattr(pg_row, field):
                report["findings"].append({"severity": "high", "kind": f"{field}_mismatch", "client_id": key[0], "phone": key[1]})
        if at_row.messages != pg_row.messages:
            report["findings"].append({"severity": "high", "kind": "message_history_mismatch", "client_id": key[0], "phone": key[1]})
        if at_row.created_at != pg_row.created_at:
            report["findings"].append({"severity": "medium", "kind": "created_timestamp_mismatch", "client_id": key[0], "phone": key[1]})
    report["high_severity_count"] = sum(item["severity"] == "high" for item in report["findings"])
    return report


CONFLICT_RULES = {
    "ownership": "Never infer or reassign client ownership; stop on mismatch.",
    "lead": "Airtable remains canonical until approved cutover; missing Postgres lead may be backfilled by tenant plus normalized phone.",
    "stage_takeover": "Airtable wins during dual mode; record mismatch and repair Postgres only through reviewed staging backfill.",
    "messages": "Provider ID is canonical when present; otherwise use tenant, normalized phone, timestamp, direction, type, and body fingerprint.",
    "timestamps": "Preserve source timestamps; timezone/format-only differences are medium severity, never silently overwritten.",
}

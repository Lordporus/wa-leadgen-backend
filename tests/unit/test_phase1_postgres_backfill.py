from types import SimpleNamespace

import pytest

from scripts.phase1_postgres_backfill import (
    PRODUCTION_CONFIRMATION,
    build_preview,
    validate_production_apply,
)


def _lead(lead_id: int, phone: str, status: str = "New Lead"):
    return SimpleNamespace(
        id=lead_id,
        phone=phone,
        name="Lead",
        source="Inbound WhatsApp",
        status=status,
        business_name=None,
        lead_score=None,
        is_human_takeover=False,
        created_at=None,
    )


def _record(record_id: str, phone: str, *, name: str = "Lead", status: str = "New Lead"):
    return {
        "id": record_id,
        "fields": {
            "Name": name,
            "Phone number type": phone,
            "Source": "Inbound WhatsApp",
            "Status": status,
        },
    }


def test_preview_separates_insert_match_conflict_and_suspicious_rows():
    records = [
        _record("rec-insert", "91111"),
        _record("rec-match", "92222"),
        _record("rec-conflict", "93333", status="Booked"),
        _record("rec-dup-a", "94444"),
        _record("rec-dup-b", "+94-444"),
        _record("rec-test", "95555", name="Test Lead"),
        _record("rec-blank", ""),
    ]
    leads = [
        _lead(2, "92222"),
        _lead(3, "93333", status="Contacted"),
    ]

    preview, approved_ids = build_preview(records, leads)

    assert preview["counts"] == {
        "airtable": 7,
        "postgres": 2,
        "to_insert": 1,
        "already_matching": 1,
        "conflicts": 1,
        "suspicious": 4,
    }
    assert approved_ids == {"rec-insert", "rec-match", "rec-conflict"}
    assert preview["records_with_conflicts"][0]["fields"] == ["status"]
    assert {
        reason
        for item in preview["suspicious_records"]
        for reason in item["reasons"]
    } >= {"duplicate_normalized_phone", "test_marker", "missing_phone"}


def test_explicit_record_approval_can_include_a_suspicious_unique_row():
    record = _record("rec-test", "95555", name="Test Lead")

    preview, approved_ids = build_preview([record], [], {"rec-test"})

    assert preview["counts"]["to_insert"] == 1
    assert preview["counts"]["suspicious"] == 0
    assert approved_ids == {"rec-test"}


def test_production_apply_requires_backup_manifest(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("BACKFILL_APPLY_CONFIRMATION", PRODUCTION_CONFIRMATION)
    monkeypatch.setenv("BACKFILL_APPROVAL_ID", "phase1-user-approval")

    with pytest.raises(RuntimeError, match="backup-manifest"):
        validate_production_apply(None, 1)

def test_blank_phone_cannot_be_explicitly_approved():
    record = _record("rec-blank", "", name="Test Lead")

    preview, approved_ids = build_preview([record], [], {"rec-blank"})

    assert preview["counts"]["suspicious"] == 1
    assert approved_ids == set()


def test_only_one_record_per_duplicate_phone_can_be_approved():
    records = [
        _record("rec-dup-a", "94444"),
        _record("rec-dup-b", "+94-444"),
    ]

    with pytest.raises(RuntimeError, match="at most one"):
        build_preview(records, [], {"rec-dup-a", "rec-dup-b"})


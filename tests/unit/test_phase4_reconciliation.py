from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.reconciliation import airtable_snapshot, classify_airtable_record, eligible_for_backfill, message_fingerprint, normalize_phone, normalize_timestamp, postgres_snapshot, reconcile
from app.store.store import DualWriteStore
from scripts.backfill_airtable_postgres import _validate_apply_target, backfill


def test_reconciliation_normalizes_phone_and_reports_high_severity_mismatches():
    airtable = airtable_snapshot({"fields": {
        "Phone number type": "+91 99999-00000",
        "Status": "Contacted",
        "is_human_takeover": False,
        "Last_Message": "[2026-07-28 10:00:00] INBOUND (text): hello",
    }}, 7)
    postgres = SimpleNamespace(
        client_id=7, phone="919999900000", status="Qualified", is_human_takeover=True,
        created_at=None, updated_at=None,
        messages=[],
    )
    report = reconcile([airtable], [postgres_snapshot(postgres)])

    assert normalize_phone("+91 99999-00000") == "919999900000"
    assert report["high_severity_count"] == 3
    assert {item["kind"] for item in report["findings"]} >= {
        "status_mismatch", "is_human_takeover_mismatch", "message_history_mismatch"
    }


def test_reconciliation_reports_missing_lead_and_duplicate_phone():
    one = airtable_snapshot({"fields": {"Phone number type": "91999", "Status": "New Lead"}}, 1)
    two = airtable_snapshot({"fields": {"Phone number type": "+91-999", "Status": "New Lead"}}, 1)
    report = reconcile([one, two], [])

    assert report["high_severity_count"] >= 2
    assert {item["kind"] for item in report["findings"]} >= {"duplicate_normalized_phone", "missing_lead"}


def test_trusted_preservation_set_excludes_only_operator_attested_airtable_only_records():
    real = airtable_snapshot({"fields": {"Phone number type": "91999", "Status": "New Lead"}}, 1, classification="real_production")
    test = airtable_snapshot({"fields": {"Phone number type": "15551234567", "Status": "New Lead"}}, 1, classification="known_development_test", classification_reason="operator_attested")
    report = reconcile([real, test], [])

    assert report["counts"]["test_excluded"] == 1
    assert report["high_severity_count"] == 1
    assert classify_airtable_record({"fields": {"Phone number type": "15551234567"}}, {"91999"})[0] == "ambiguous_unclassified"
    assert classify_airtable_record({"fields": {"Phone number type": "15551234567"}}, {"91999"}, trust_postgres_preservation_set=True)[0] == "known_development_test"


def test_legacy_message_comparison_ignores_timezone_case_and_provider_id():
    airtable = {"timestamp": "2026-07-28 10:00:00", "direction": "INBOUND", "kind": "text", "body": "hello"}
    postgres = {"timestamp": "2026-07-28 10:00:00+00:00", "direction": "inbound", "kind": "TEXT", "body": "hello", "provider_id": "wamid.1"}

    assert normalize_timestamp("2026-07-28T10:00:00+00:00") == "2026-07-28 10:00:00"
    assert message_fingerprint(airtable) == message_fingerprint(postgres)


def test_backfill_blocks_test_rows_without_an_explicit_record_approval():
    assert not eligible_for_backfill("rec-test", "known_development_test")
    assert eligible_for_backfill("rec-test", "known_development_test", {"rec-test"})
    assert eligible_for_backfill("rec-real", "real_production")


def test_backfill_apply_is_staging_only(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    try:
        backfill(1, apply=True)
    except RuntimeError as error:
        assert "staging" in str(error)
    else:
        raise AssertionError("production backfill apply must be rejected")


def test_backfill_apply_rejects_a_production_database_even_when_env_says_staging(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("BACKFILL_APPLY_CONFIRMATION", "staging-only")
    monkeypatch.setenv("PRODUCTION_DATABASE_IDENTITY", "db.production.supabase.co:5432/postgres")

    try:
        _validate_apply_target("postgresql://user:secret@db.production.supabase.co:5432/postgres")
    except RuntimeError as error:
        assert "production database" in str(error)
    else:
        raise AssertionError("production database must be rejected even in a mislabelled staging environment")


def test_backfill_apply_requires_explicit_staging_confirmation_and_production_identity(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("BACKFILL_APPLY_CONFIRMATION", raising=False)
    monkeypatch.delenv("PRODUCTION_DATABASE_IDENTITY", raising=False)

    try:
        _validate_apply_target("postgresql://user:secret@db.staging.supabase.co:5432/postgres")
    except RuntimeError as error:
        assert "BACKFILL_APPLY_CONFIRMATION" in str(error)
    else:
        raise AssertionError("write confirmation must be required")


def test_dual_write_failure_is_contained_and_reported(monkeypatch):
    primary = MagicMock()
    primary.add_lead.return_value = {"id": "rec-primary"}
    secondary = MagicMock()
    secondary.add_lead.side_effect = RuntimeError("shadow unavailable")
    recorded = MagicMock()
    monkeypatch.setattr(DualWriteStore, "_record_failure", recorded)
    store = DualWriteStore(primary, secondary)

    result = store.add_lead("Lead", "91999900000", client_id=7)

    assert result == {"id": "rec-primary"}
    recorded.assert_called_once()
    assert recorded.call_args.kwargs["client_id"] == 7
    assert recorded.call_args.kwargs["reference"] == "91999900000"

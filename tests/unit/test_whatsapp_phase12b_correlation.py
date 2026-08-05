from types import SimpleNamespace

import pytest

from app.services import whatsapp_observability, whatsapp_queue


def _correlation_session(*, original: str | None):
    status_receipt = SimpleNamespace(
        id=2,
        client_id=9,
        event_kind="status",
        event_id="provider-message",
        correlation_id="arrival-race-correlation",
    )
    inbound_receipt = SimpleNamespace(id=1, client_id=9, correlation_id=original)
    intent = SimpleNamespace(
        client_id=9,
        provider_message_id="provider-message",
        inbound_event_id=1,
        correlation_id=None,
    )

    class Query:
        def __init__(self, model):
            self.model = model
            self.filters = {}

        def filter_by(self, **kwargs):
            self.filters = kwargs
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            if self.model is whatsapp_queue.WhatsAppOutboundIntent:
                return intent
            if self.filters.get("event_kind") == "status":
                return status_receipt
            if self.filters.get("id") == 1:
                return inbound_receipt
            return None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, model):
            return Query(model)

        def commit(self):
            return None

    return Session, status_receipt, intent


def test_status_arrival_race_restores_original_outbound_correlation(monkeypatch):
    session, status_receipt, intent = _correlation_session(original="original-outbound-correlation")
    monkeypatch.setattr(whatsapp_queue.database, "SessionLocal", session)
    captured = []

    @whatsapp_queue._correlated_job
    def worker(envelope):
        captured.append((envelope["correlation_id"], whatsapp_observability.current_correlation_id()))

    worker({"event_kind": "status", "event_id": "provider-message", "tenant_id": 9, "correlation_id": "arrival-race-correlation"})

    assert captured == [("original-outbound-correlation", "original-outbound-correlation")]
    assert status_receipt.correlation_id == "original-outbound-correlation"
    assert intent.correlation_id == "original-outbound-correlation"


def test_worker_fails_closed_when_durable_correlation_cannot_be_recovered(monkeypatch):
    session, _status_receipt, _intent = _correlation_session(original=None)
    monkeypatch.setattr(whatsapp_queue.database, "SessionLocal", session)

    with pytest.raises(whatsapp_queue.PermanentWebhookError, match="correlation"):
        whatsapp_queue._restore_durable_correlation({"event_kind": "status", "event_id": "provider-message", "tenant_id": 9, "correlation_id": None})

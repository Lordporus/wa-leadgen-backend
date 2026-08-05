from requests import Response
from requests.exceptions import ConnectionError, HTTPError

from app.services import whatsapp_outbox


def test_uncertain_provider_failures_are_never_safe_to_resend():
    assert whatsapp_outbox._is_uncertain_provider_error(ConnectionError("offline")) is True

    response = Response()
    response.status_code = 503
    assert whatsapp_outbox._is_uncertain_provider_error(HTTPError(response=response)) is True

    response.status_code = 400
    assert whatsapp_outbox._is_uncertain_provider_error(HTTPError(response=response)) is False
    assert whatsapp_outbox._send_failure_is_uncertain(
        RuntimeError("database commit failed"),
        provider_accepted=True,
    ) is True


def test_status_order_is_monotonic():
    assert whatsapp_outbox._STATUS_ORDER["sent"] < whatsapp_outbox._STATUS_ORDER["delivered"]
    assert whatsapp_outbox._STATUS_ORDER["delivered"] < whatsapp_outbox._STATUS_ORDER["read"]

    assert whatsapp_outbox._next_provider_status("sent", "failed") == "failed"
    assert whatsapp_outbox._next_provider_status("failed", "delivered") is None
    assert whatsapp_outbox._next_provider_status("delivered", "failed") is None


def test_replay_refuses_unknown_send_outcomes(monkeypatch):
    class Query:
        def filter_by(self, **_):
            return self

        def with_for_update(self):
            return self

        def one_or_none(self):
            return type("Intent", (), {"state": "unknown", "body": "reply"})()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def query(self, _):
            return Query()

    monkeypatch.setattr(whatsapp_outbox.database, "SessionLocal", Session)
    try:
        whatsapp_outbox.replay_outbound_intent(intent_id=4, client_id=7)
    except ValueError as exc:
        assert "Only failed intents" in str(exc)
    else:
        raise AssertionError("unknown provider outcomes must not be replayed")


def test_failed_replay_is_forced_through_resumed_validation_worker(monkeypatch):
    from app.api import runtime

    intent = type("Intent", (), {"state": "failed", "body": "persisted", "failure_category": "provider_exception", "failure_reason": "offline", "inbound_event_id": 8, "client_id": 7, "correlation_id": None})()
    event = type("Event", (), {"correlation_id": "durable-correlation"})()

    class Query:
        def filter_by(self, **_): return self
        def with_for_update(self): return self
        def one_or_none(self):
            return event if self.model is whatsapp_outbox.WhatsAppWebhookEvent else intent
        def __init__(self, model): self.model = model

    class Session:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def query(self, model): return Query(model)
        def commit(self): pass

    queued = []

    class Queue:
        def enqueue(self, function, **kwargs):
            queued.append((function, kwargs))
            return type("Job", (), {"id": "job-phase9-replay"})()

    monkeypatch.setattr(whatsapp_outbox.database, "SessionLocal", Session)
    monkeypatch.setattr(runtime, "webhook_queue", Queue())
    assert whatsapp_outbox.replay_outbound_intent(intent_id=4, client_id=7) == "job-phase9-replay"
    assert intent.correlation_id == "durable-correlation"
    assert queued == [(whatsapp_outbox.process_outbound_intent, {"intent_id": 4, "client_id": 7})]


def test_crashed_generation_recovers_or_dispatches_persisted_body(monkeypatch):
    def claim(intent):
        class Query:
            def filter_by(self, **_): return self
            def with_for_update(self): return self
            def one_or_none(self): return intent
        class Session:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def query(self, _): return Query()
            def commit(self): pass
        monkeypatch.setattr(whatsapp_outbox.database, "SessionLocal", Session)
        return whatsapp_outbox.claim_for_generation(intent_id=1, client_id=7)

    assert claim(type("Intent", (), {"state": "generating", "body": None, "claimed_at": None})()) == "generate"
    assert claim(type("Intent", (), {"state": "generating", "body": "persisted", "claimed_at": None})()) == "dispatch"


def test_opt_out_is_persisted_before_any_outbound_claim(monkeypatch):
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(whatsapp_outbox.whatsapp_policy, "record_inbound_opt_out", record)
    assert whatsapp_outbox.record_inbound_opt_out(
        client_id=7,
        recipient_phone="1555",
        text="STOP",
        inbound_event_id="wamid.inbound",
    )
    assert calls == [{
        "client_id": 7,
        "phone": "1555",
        "text": "STOP",
        "inbound_event_id": "wamid.inbound",
    }]

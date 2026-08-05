from types import SimpleNamespace

import pytest
from requests import Response
from requests.exceptions import ConnectionError as RequestsConnectionError, HTTPError

from app.services import jobs
from app.services import whatsapp_queue
from app.services import whatsapp_operations


@pytest.fixture(autouse=True)
def _enabled_worker_control(monkeypatch):
    monkeypatch.setattr(whatsapp_operations, "enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(whatsapp_queue, "_restore_durable_correlation", lambda envelope: dict(envelope))



def _envelope(kind="message"):
    return {
        "event_id": "wamid.phase5",
        "event_kind": kind,
        "tenant_id": 7,
        "phone_number_id": "phone-id",
        "correlation_id": "cid-1",
        "attempt": 0,
        "payload": {"id": "wamid.phase5", "from": "15550000000", "type": "text", "text": {"body": "hello"}}
        if kind == "message" else {"id": "wamid.phase5", "status": "read"},
    }


def test_worker_executes_message_only_in_worker(monkeypatch):
    states = []
    calls = []
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: states.append((args, kwargs)))
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: calls.append((args, kwargs)))

    whatsapp_queue.process_webhook_event(_envelope())

    assert calls == [(
        ("phone-id", _envelope()["payload"]),
        {"current_client_id": 7, "inbound_event_id": "wamid.phase5", "correlation_id": "cid-1"},
    )]
    assert [state[0][3] for state in states] == ["processing", "processed"]


def test_enqueue_creates_a_durable_receipt_and_bounded_rq_job(monkeypatch):
    class Query:
        def filter_by(self, **_):
            return self

        def one_or_none(self):
            return None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def query(self, _):
            return Query()

        def add(self, _):
            return None

        def flush(self):
            return None

        def commit(self):
            return None

    class Queue:
        def __init__(self):
            self.calls = []

        def enqueue(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(id="rq-job-1")

    queue = Queue()
    monkeypatch.setattr(whatsapp_queue.database, "SessionLocal", Session)
    monkeypatch.setattr(whatsapp_queue, "webhook_queue", queue)

    correlation_id = whatsapp_queue.enqueue_event(
        kind="message", payload=_envelope()["payload"], phone_number_id="phone-id", client_id=7
    )

    assert correlation_id
    _, kwargs = queue.calls[0]
    assert kwargs["job_timeout"] == whatsapp_queue.WHATSAPP_RQ_JOB_TIMEOUT
    assert kwargs["retry"].max == whatsapp_queue.WHATSAPP_RQ_MAX_RETRIES
    assert kwargs["retry"].intervals == list(whatsapp_queue.WHATSAPP_RQ_RETRY_INTERVALS)
    assert kwargs["meta"] == {"whatsapp_initial_retries": whatsapp_queue.WHATSAPP_RQ_MAX_RETRIES}


def test_concurrent_receipt_conflict_reconciles_existing_event(monkeypatch):
    existing = SimpleNamespace(correlation_id="existing-correlation")
    class Query:
        def __init__(self): self.calls = 0
        def filter_by(self, **_): return self
        def one_or_none(self):
            self.calls += 1
            return None if self.calls == 1 else existing
        def one(self): return existing
    query = Query()
    class Session:
        commits = 0
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def query(self, _): return query
        def add(self, _): pass
        def flush(self): pass
        def commit(self):
            self.commits += 1
            if self.commits == 1:
                raise whatsapp_queue.IntegrityError("insert", {}, Exception())
        def rollback(self): pass
    monkeypatch.setattr(whatsapp_queue.database, "SessionLocal", Session)
    monkeypatch.setattr(whatsapp_queue, "webhook_queue", object())
    assert whatsapp_queue.enqueue_event(kind="message", payload=_envelope()["payload"], phone_number_id="phone-id", client_id=7) == "existing-correlation"


def test_queued_envelope_can_execute_after_worker_restart(monkeypatch):
    """A serialized RQ payload has no web-process state and can be resumed."""
    calls = []
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: calls.append((args, kwargs)))

    restored_envelope = dict(_envelope())  # equivalent payload read by a new worker process
    whatsapp_queue.process_webhook_event(restored_envelope)

    assert calls[0][0][0] == "phone-id"
    assert calls[0][1]["current_client_id"] == 7


def test_worker_routes_status_events_through_same_durable_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(jobs, "process_status_update", lambda *args, **kwargs: calls.append((args, kwargs)))

    whatsapp_queue.process_webhook_event(_envelope("status"))

    assert calls == [(
        ({"id": "wamid.phase5", "status": "read"},),
        {"current_client_id": 7, "phone_number_id": "phone-id", "require_known_intent": True, "correlation_id": "cid-1"},
    )]


def test_retryable_failure_is_re_raised_for_bounded_rq_retry(monkeypatch):
    retry_marks = []
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(whatsapp_queue, "_mark_retry_or_dead_letter", lambda *args: retry_marks.append(args))
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: (_ for _ in ()).throw(RequestsConnectionError("offline")))

    with pytest.raises(RequestsConnectionError):
        whatsapp_queue.process_webhook_event(_envelope())
    assert retry_marks[0][:3] == (7, "message", "wamid.phase5")


def test_permanent_failure_is_dead_lettered_without_retry(monkeypatch):
    states = []
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: states.append((args, kwargs)))
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad payload")))

    with pytest.raises(whatsapp_queue.PermanentWebhookError):
        whatsapp_queue.process_webhook_event(_envelope())
    assert states[-1][0][3] == "dead_letter"
    assert states[-1][1]["dead_letter"] is True


def test_permanent_provider_4xx_is_dead_lettered_without_retry(monkeypatch):
    states = []
    response = Response()
    response.status_code = 400
    error = HTTPError("bad request", response=response)
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: states.append((args, kwargs)))
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(whatsapp_queue.PermanentWebhookError):
        whatsapp_queue.process_webhook_event(_envelope())
    assert states[-1][0][3] == "dead_letter"


def test_transient_provider_5xx_is_retried(monkeypatch):
    retry_marks = []
    response = Response()
    response.status_code = 503
    error = HTTPError("unavailable", response=response)
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(whatsapp_queue, "_mark_retry_or_dead_letter", lambda *args: retry_marks.append(args))
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(HTTPError):
        whatsapp_queue.process_webhook_event(_envelope())
    assert retry_marks[0][:3] == (7, "message", "wamid.phase5")


@pytest.mark.parametrize("retries_left, expected_state", [(1, "queued"), (0, "dead_letter")])
def test_retry_state_moves_to_dead_letter_only_after_final_attempt(monkeypatch, retries_left, expected_state):
    calls = []
    monkeypatch.setattr(whatsapp_queue, "get_current_job", lambda: SimpleNamespace(retries_left=retries_left))
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: calls.append((args, kwargs)))

    whatsapp_queue._mark_retry_or_dead_letter(7, "message", "wamid.phase5", RequestsConnectionError("offline"))

    assert calls[0][0][3] == expected_state
    assert calls[0][1]["dead_letter"] is (retries_left == 0)


def test_processing_persists_actual_rq_retry_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(
        whatsapp_queue,
        "get_current_job",
        lambda: SimpleNamespace(retries_left=1, meta={"whatsapp_initial_retries": 3}),
    )
    monkeypatch.setattr(whatsapp_queue, "_mark_state", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(jobs, "process_webhook_message", lambda *args, **kwargs: None)

    whatsapp_queue.process_webhook_event(_envelope())

    assert calls[0][1]["retry_attempt"] == 2


def test_legacy_direct_replay_path_is_disabled():
    with pytest.raises(RuntimeError, match="protected tenant API"):
        whatsapp_queue.replay_dead_letter(receipt_id=10)

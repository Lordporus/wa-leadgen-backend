import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

import worker


def test_worker_requires_explicit_single_rq_concurrency(monkeypatch):
    monkeypatch.setattr(worker, "WHATSAPP_RQ_WORKER_CONCURRENCY", 2)

    with pytest.raises(RuntimeError, match="must be 1"):
        worker.validate_worker_configuration()


def test_worker_startup_does_not_enqueue_a_second_active_sequence_tick():
    queue = MagicMock()
    queue.fetch_job.return_value = SimpleNamespace(get_status=lambda: "scheduled")
    worker.ensure_sequence_tick(queue)
    queue.enqueue.assert_not_called()


def test_worker_startup_enqueues_sequence_tick_when_no_tick_exists():
    queue = MagicMock()
    queue.fetch_job.return_value = None
    worker.ensure_sequence_tick(queue)
    assert queue.enqueue.call_args.kwargs["job_id"] == worker.SEQUENCE_TICK_JOB_ID

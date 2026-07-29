import pytest

import worker


def test_worker_requires_explicit_single_rq_concurrency(monkeypatch):
    monkeypatch.setattr(worker, "WHATSAPP_RQ_WORKER_CONCURRENCY", 2)

    with pytest.raises(RuntimeError, match="must be 1"):
        worker.validate_worker_configuration()

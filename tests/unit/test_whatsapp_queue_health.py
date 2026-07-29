import inspect

from app.api.routers import health


def test_health_requires_a_reachable_queue_consumer(monkeypatch):
    class Redis:
        def ping(self):
            return True

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, _):
            return None

    class Queue:
        count = 2

    monkeypatch.setattr(health, "redis_conn", Redis())
    monkeypatch.setattr(health, "webhook_queue", Queue())
    monkeypatch.setattr(health, "SessionLocal", Session)
    monkeypatch.setattr(health.Worker, "all", lambda connection: [])

    result = inspect.unwrap(health.health_check)(None, None)

    assert result["status"] == "degraded"
    assert result["whatsapp_queue"] == {"ready": False, "depth": 2, "workers": 0}

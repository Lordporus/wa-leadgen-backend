import asyncio
import hashlib
import hmac
import inspect
import json
from urllib.parse import urlencode

import pytest
from fastapi import BackgroundTasks, HTTPException, Request, Response

from app.api.routers import whatsapp as whatsapp_router


def _app_secret() -> str:
    value = whatsapp_router.WHATSAPP_APP_SECRET
    assert value
    return value


def _signed_request(payload: dict) -> Request:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(
        _app_secret().encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhook",
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-hub-signature-256", f"sha256={signature}".encode()),
            ],
        },
        receive,
    )


def _call_webhook(payload: dict):
    tasks = BackgroundTasks()
    endpoint = inspect.unwrap(whatsapp_router.receive_message)
    result = asyncio.run(endpoint(_signed_request(payload), Response(), tasks))
    return result, tasks


def test_signature_verification_accepts_valid_and_rejects_invalid():
    body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(
        _app_secret().encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    assert whatsapp_router.verify_signature(body, f"sha256={digest}") is True
    assert whatsapp_router.verify_signature(body, "sha256=invalid") is False
    assert whatsapp_router.verify_signature(body, "") is False


def test_missing_signature_or_secret_fails_closed(monkeypatch):
    body = b'{"object":"whatsapp_business_account"}'
    assert whatsapp_router.verify_signature(body, None) is False

    monkeypatch.setattr(whatsapp_router, "WHATSAPP_APP_SECRET", None)
    assert whatsapp_router.verify_signature(body, "sha256=unused") is False


@pytest.mark.parametrize("challenge", [None, "not-an-integer"])
def test_webhook_verification_rejects_malformed_challenge(challenge):
    query = {
        "hub.mode": "subscribe",
        "hub.verify_token": whatsapp_router.WHATSAPP_VERIFY_TOKEN,
    }
    if challenge is not None:
        query["hub.challenge"] = challenge
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/webhook",
            "query_string": urlencode(query).encode(),
            "headers": [],
        }
    )
    endpoint = inspect.unwrap(whatsapp_router.verify_webhook)

    with pytest.raises(HTTPException) as error:
        endpoint(request, Response())

    assert error.value.status_code == 400


def test_invalid_webhook_signature_fails_closed():
    request = _signed_request({"object": "whatsapp_business_account"})
    request.scope["headers"] = [(b"x-hub-signature-256", b"sha256=invalid")]
    endpoint = inspect.unwrap(whatsapp_router.receive_message)

    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(request, Response(), BackgroundTasks()))

    assert error.value.status_code == 403


def test_duplicate_message_is_not_enqueued(monkeypatch):
    class DuplicateRedis:
        def ping(self):
            return True

        def setnx(self, key, value):
            return False

        def expire(self, key, seconds):
            raise AssertionError("Duplicate keys must not be refreshed")

    monkeypatch.setattr(whatsapp_router, "redis_conn", DuplicateRedis())
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "test-number"},
                    "messages": [{"id": "wamid.duplicate", "from": "15550000000"}],
                }
            }]
        }],
    }

    result, tasks = _call_webhook(payload)

    assert result == {"status": "queued"}
    assert tasks.tasks == []


def test_fresh_message_and_status_are_queued_without_running_providers(monkeypatch):
    monkeypatch.setattr(whatsapp_router, "redis_conn", None)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": "test-number"},
                    "messages": [{"id": "", "from": "15550000000", "type": "text"}],
                    "statuses": [{"id": "wamid.status", "status": "delivered"}],
                }
            }]
        }],
    }

    result, tasks = _call_webhook(payload)

    assert result == {"status": "queued"}
    assert len(tasks.tasks) == 2

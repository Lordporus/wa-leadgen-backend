import asyncio
import hashlib
import hmac
import inspect
import json
from urllib.parse import urlencode
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request, Response

from app.api.routers import whatsapp as whatsapp_router
from app.services import jobs


def _app_secret() -> str:
    app_secret = whatsapp_router.WHATSAPP_APP_SECRET
    assert isinstance(app_secret, str)
    return app_secret


def _signed_request(payload: dict) -> Request:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(_app_secret().encode(), body, hashlib.sha256).hexdigest()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/webhook", "headers": [
        (b"content-type", b"application/json"),
        (b"x-hub-signature-256", f"sha256={signature}".encode()),
    ]}, receive)


def _call_webhook(payload: dict):
    endpoint = inspect.unwrap(whatsapp_router.receive_message)
    return asyncio.run(endpoint(_signed_request(payload), Response()))


def _context(client_id=7):
    return type("Context", (), {"client": type("Client", (), {"id": client_id})()})()


def test_signature_verification_accepts_valid_and_rejects_invalid():
    body = b'{"object":"whatsapp_business_account"}'
    digest = hmac.new(_app_secret().encode(), body, hashlib.sha256).hexdigest()
    assert whatsapp_router.verify_signature(body, f"sha256={digest}") is True
    assert whatsapp_router.verify_signature(body, "sha256=invalid") is False


def test_invalid_webhook_signature_fails_closed():
    request = _signed_request({"object": "whatsapp_business_account"})
    request.scope["headers"] = [(b"x-hub-signature-256", b"sha256=invalid")]
    with pytest.raises(HTTPException) as error:
        asyncio.run(inspect.unwrap(whatsapp_router.receive_message)(request, Response()))
    assert error.value.status_code == 403


@pytest.mark.parametrize("challenge", [None, "not-an-integer"])
def test_webhook_verification_rejects_malformed_challenge(challenge):
    query = {"hub.mode": "subscribe", "hub.verify_token": whatsapp_router.WHATSAPP_VERIFY_TOKEN}
    if challenge is not None:
        query["hub.challenge"] = challenge
    request = Request({"type": "http", "method": "GET", "path": "/webhook", "query_string": urlencode(query).encode(), "headers": []})
    with pytest.raises(HTTPException) as error:
        inspect.unwrap(whatsapp_router.verify_webhook)(request, Response())
    assert error.value.status_code == 400


def test_valid_message_acknowledges_after_enqueue_without_business_calls(monkeypatch):
    enqueued = []
    monkeypatch.setattr(whatsapp_router.tenant, "resolve_context_by_phone_id", lambda _: _context())
    monkeypatch.setattr(whatsapp_router, "_enqueue_or_retry", lambda **kwargs: enqueued.append(kwargs))
    result = _call_webhook({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "test-number"},
        "messages": [{"id": "wamid.message", "from": "15550000000", "type": "text", "text": {"body": "hello"}}],
    }}]}]})
    assert result == {"status": "queued"}
    assert enqueued == [{"kind": "message", "phone_number_id": "test-number", "client_id": 7,
                         "payload": {"id": "wamid.message", "from": "15550000000", "type": "text", "text": {"body": "hello"}}}]


def test_message_and_status_use_the_same_durable_enqueue_path(monkeypatch):
    enqueued = []
    monkeypatch.setattr(whatsapp_router.tenant, "resolve_context_by_phone_id", lambda _: _context())
    monkeypatch.setattr(whatsapp_router, "_enqueue_or_retry", lambda **kwargs: enqueued.append(kwargs))
    assert _call_webhook({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "test-number"},
        "messages": [{"id": "wamid.message", "from": "15550000000", "type": "text"}],
        "statuses": [{"id": "wamid.status", "status": "read"}],
    }}]}]}) == {"status": "queued"}
    assert [item["kind"] for item in enqueued] == ["message", "status"]


def test_unknown_phone_is_not_acknowledged_as_queued(monkeypatch):
    enqueue = MagicMock()
    monkeypatch.setattr(whatsapp_router.tenant, "resolve_context_by_phone_id", lambda _: None)
    monkeypatch.setattr(whatsapp_router, "_enqueue_or_retry", enqueue)
    with pytest.raises(HTTPException) as error:
        _call_webhook({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "unknown"}, "messages": [{"id": "wamid.unknown", "from": "1555"}],
        }}]}]})
    assert error.value.status_code == 403
    enqueue.assert_not_called()


@pytest.mark.parametrize("processor, payload", [
    (jobs.process_webhook_message, ("verified-phone", {"id": "wamid.stale", "from": "15550000000", "type": "text", "text": {"body": "hello"}})),
    (jobs.process_status_update, ({"id": "wamid.stale", "status": "delivered"}, 8, "verified-phone")),
])
def test_forged_or_stale_job_tenant_is_rejected_before_store_access(monkeypatch, processor, payload):
    monkeypatch.setattr(jobs.tenant, "resolve_context_by_phone_id", lambda _: _context())
    monkeypatch.setattr(jobs, "get_store", lambda: (_ for _ in ()).throw(AssertionError("must not reach store")))
    if processor is jobs.process_webhook_message:
        processor(*payload, current_client_id=8)
    else:
        status_data, current_client_id, phone_number_id = payload
        processor(status_data, current_client_id=current_client_id, phone_number_id=phone_number_id)

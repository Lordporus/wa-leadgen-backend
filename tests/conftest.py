import os
import http.client
import socket
import urllib.request

import pytest
import requests

_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex


# Collection must never inherit production credentials from backend/.env.
_OFFLINE_ENV = {
    "APP_ENV": "test",
    "AIRTABLE_API_KEY": "",
    "AIRTABLE_BASE_ID": "",
    "AIRTABLE_TABLE_NAME": "",
    "APIFY_API_TOKEN": "",
    "CALENDLY_API_TOKEN": "",
    "CLIENT_ID": "1",
    "DATABASE_URL": "",
    "EMAIL_PLATFORM_ENABLED": "false",
    "GEMINI_API_KEY": "",
    "NINEROUTER_API_KEY": "",
    "NINEROUTER_BASE_URL": "https://offline.invalid",
    "REDIS_URL": "",
    "RESEND_API_KEY": "",
    "RESEND_WEBHOOK_SECRET": "",
    "SENTRY_DSN": "",
    "WHATSAPP_ACCESS_TOKEN": "",
    "WHATSAPP_APP_SECRET": "offline-test-secret",
    "WHATSAPP_BUSINESS_ACCOUNT_ID": "",
    "WHATSAPP_PHONE_NUMBER_ID": "",
    "WHATSAPP_VERIFY_TOKEN": "offline-test-token",
}
for _name, _value in _OFFLINE_ENV.items():
    os.environ[_name] = _value


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Fail every offline unit test that attempts a real network connection."""
    def _blocked(*args, **kwargs):
        raise AssertionError("External network access is forbidden in offline unit tests")

    def _loopback_only_connect(sock, address):
        host = address[0] if isinstance(address, tuple) and address else None
        if host in {"127.0.0.1", "::1", "localhost"}:
            return _ORIGINAL_SOCKET_CONNECT(sock, address)
        return _blocked(sock, address)

    def _loopback_only_connect_ex(sock, address):
        host = address[0] if isinstance(address, tuple) and address else None
        if host in {"127.0.0.1", "::1", "localhost"}:
            return _ORIGINAL_SOCKET_CONNECT_EX(sock, address)
        return _blocked(sock, address)

    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _loopback_only_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _loopback_only_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", _blocked)

    from app.clients.gemini_client import GeminiClient
    from app.clients.whatsapp_client import WhatsAppClient

    for method_name in ("send_message", "send_template", "submit_template", "get_template"):
        monkeypatch.setattr(WhatsAppClient, method_name, _blocked)

    for method_name in (
        "generate_response_with_history",
        "extract_lead_info",
        "score_lead",
    ):
        monkeypatch.setattr(GeminiClient, method_name, _blocked)

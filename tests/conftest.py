import os
import socket

import pytest
import requests


# Collection must never inherit production credentials from backend/.env.
_OFFLINE_ENV = {
    "AIRTABLE_API_KEY": "",
    "AIRTABLE_BASE_ID": "",
    "AIRTABLE_TABLE_NAME": "",
    "APIFY_API_TOKEN": "",
    "CALENDLY_API_TOKEN": "",
    "DATABASE_URL": "",
    "EMAIL_PLATFORM_ENABLED": "false",
    "GEMINI_API_KEY": "",
    "NINEROUTER_API_KEY": "",
    "REDIS_URL": "",
    "RESEND_API_KEY": "",
    "RESEND_WEBHOOK_SECRET": "",
    "SENTRY_DSN": "",
    "WHATSAPP_ACCESS_TOKEN": "",
    "WHATSAPP_APP_SECRET": "offline-test-secret",
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

    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

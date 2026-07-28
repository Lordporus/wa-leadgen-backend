import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import requests


def test_test_environment_cannot_inherit_provider_credentials():
    for name in (
        "AIRTABLE_API_KEY",
        "APIFY_API_TOKEN",
        "DATABASE_URL",
        "GEMINI_API_KEY",
        "NINEROUTER_API_KEY",
        "REDIS_URL",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
    ):
        assert os.environ[name] == ""


def test_test_config_does_not_read_dotenv():
    with tempfile.TemporaryDirectory(
        prefix=".pytest-offline-",
        dir=Path.cwd(),
    ) as temporary_directory:
        working_directory = Path(temporary_directory)
        (working_directory / ".env").write_text(
            "ISOLATION_SENTINEL=production-value-must-not-load\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["APP_ENV"] = "test"
        env.pop("ISOLATION_SENTINEL", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; import app.core.config; "
                    "assert os.getenv('ISOLATION_SENTINEL') is None"
                ),
            ],
            cwd=working_directory,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr


def test_requests_network_is_blocked():
    with pytest.raises(AssertionError, match="External network access"):
        requests.get("https://offline.invalid", timeout=1)


def test_socket_network_is_blocked():
    with pytest.raises(AssertionError, match="External network access"):
        socket.create_connection(("offline.invalid", 443), timeout=1)


def test_whatsapp_provider_send_is_blocked():
    from app.clients.whatsapp_client import WhatsAppClient

    with pytest.raises(AssertionError, match="External network access"):
        WhatsAppClient().send_message("15550000000", "offline")


def test_ai_provider_generation_is_blocked():
    from app.clients.gemini_client import GeminiClient

    with pytest.raises(AssertionError, match="External network access"):
        GeminiClient().generate_response_with_history([], "offline")

"""
Email channel client — Resend via `requests`.

Phase E2: send_email implemented. Webhooks/inbound land in later phases.
Platform keys only (RESEND_API_KEY); BYOK deferred.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests

from app.core.config import (
    EMAIL_DAILY_CAP,
    EMAIL_DEFAULT_FROM_ADDRESS,
    EMAIL_DEFAULT_FROM_NAME,
    EMAIL_PLATFORM_ENABLED,
    EMAIL_PROVIDER,
    RESEND_API_BASE_URL,
    RESEND_API_KEY,
    email_is_configured,
)

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Raised when the provider rejects or fails a send."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class EmailSendResult:
    provider_message_id: str | None
    raw: dict[str, Any] | None = None


class EmailClient:
    """Thin adapter around the platform email provider (Resend first)."""

    def __init__(self) -> None:
        self.provider = EMAIL_PROVIDER
        self.api_key = RESEND_API_KEY
        self.api_base_url = RESEND_API_BASE_URL
        self.daily_cap = EMAIL_DAILY_CAP
        self.default_from_address = EMAIL_DEFAULT_FROM_ADDRESS
        self.default_from_name = EMAIL_DEFAULT_FROM_NAME
        self.platform_enabled = EMAIL_PLATFORM_ENABLED
        # In-process daily cap (mirrors WhatsAppClient anti-ban pattern).
        self._sends_today = 0
        self._current_date = date.today()

    def is_ready(self) -> bool:
        return email_is_configured()

    def status(self) -> dict[str, Any]:
        """Safe diagnostics — never includes the API key."""
        return {
            "provider": self.provider,
            "platform_enabled": self.platform_enabled,
            "configured": self.is_ready(),
            "api_key_set": bool(self.api_key),
            "api_base_url": self.api_base_url,
            "daily_cap": self.daily_cap,
            "sends_today": self._sends_today,
            "default_from_address": self.default_from_address or None,
            "default_from_name": self.default_from_name or None,
            "send_implemented": True,
        }

    def _check_rate_limit(self) -> bool:
        today = date.today()
        if today > self._current_date:
            self._current_date = today
            self._sends_today = 0
        if self._sends_today >= self.daily_cap:
            logger.warning(
                "Email daily send cap reached (%s). Blocking outbound email.",
                self.daily_cap,
            )
            return False
        return True

    def _format_from(self, from_address: str | None, from_name: str | None) -> str:
        address = (from_address or self.default_from_address or "").strip()
        name = (from_name or self.default_from_name or "").strip()
        if not address:
            raise EmailSendError("No from_address configured for email send")
        if name:
            # Avoid breaking From header if name contains quotes.
            safe_name = name.replace('"', "")
            return f"{safe_name} <{address}>"
        return address

    def send_email(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        from_address: str | None = None,
        from_name: str | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> EmailSendResult:
        """
        Send one email via the configured provider.

        Returns EmailSendResult with provider_message_id on success.
        Raises EmailSendError on configuration, cap, or provider failure.
        """
        if not self.is_ready():
            raise EmailSendError(
                "Email is not configured "
                "(set EMAIL_PLATFORM_ENABLED=true and RESEND_API_KEY)"
            )
        if self.provider != "resend":
            raise EmailSendError(f"Unsupported email provider: {self.provider}")

        to_addr = (to or "").strip()
        if not to_addr:
            raise EmailSendError("Missing recipient email")
        subject = (subject or "").strip()
        if not subject:
            raise EmailSendError("Missing email subject")
        if not (text or "").strip() and not (html or "").strip():
            raise EmailSendError("Email body is empty")

        if not self._check_rate_limit():
            raise EmailSendError(
                f"Daily email send cap reached ({self.daily_cap}). Try again tomorrow."
            )

        from_header = self._format_from(from_address, from_name)
        payload: dict[str, Any] = {
            "from": from_header,
            "to": [to_addr],
            "subject": subject,
        }
        if text is not None:
            payload["text"] = text
        if html:
            payload["html"] = html
        if reply_to:
            payload["reply_to"] = reply_to
        if headers:
            payload["headers"] = headers
        if tags:
            # Resend expects [{name, value}, ...]; values must be strings.
            payload["tags"] = [
                {"name": str(k)[:256], "value": str(v)[:256]} for k, v in tags.items()
            ]

        url = f"{self.api_base_url}/emails"
        req_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(url, headers=req_headers, json=payload, timeout=30)
        except requests.exceptions.RequestException as e:
            logger.error("Resend request failed: %s", e)
            raise EmailSendError(f"Email provider request failed: {e}") from e

        if response.status_code >= 400:
            logger.error(
                "Resend send failed status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise EmailSendError(
                "Email provider rejected the send",
                status_code=response.status_code,
                body=response.text[:500],
            )

        try:
            data = response.json()
        except ValueError:
            data = {}

        provider_id = data.get("id")
        self._sends_today += 1
        logger.info("Email sent to %s provider_id=%s", to_addr, provider_id)
        return EmailSendResult(provider_message_id=provider_id, raw=data)

    def fetch_received_email(self, email_id: str) -> dict[str, Any]:
        """
        Fetch full inbound email content (body/headers) from Resend Receiving API.

        Webhook `email.received` only carries metadata — body requires this call.
        GET /emails/receiving/{email_id}
        """
        if not self.is_ready():
            raise EmailSendError(
                "Email is not configured "
                "(set EMAIL_PLATFORM_ENABLED=true and RESEND_API_KEY)"
            )
        if self.provider != "resend":
            raise EmailSendError(f"Unsupported email provider: {self.provider}")

        eid = (email_id or "").strip()
        if not eid:
            raise EmailSendError("Missing received email id")

        url = f"{self.api_base_url}/emails/receiving/{eid}"
        req_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.get(url, headers=req_headers, timeout=30)
        except requests.exceptions.RequestException as e:
            logger.error("Resend receiving fetch failed: %s", e)
            raise EmailSendError(f"Email provider request failed: {e}") from e

        if response.status_code >= 400:
            logger.error(
                "Resend receiving get failed status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise EmailSendError(
                "Failed to fetch received email",
                status_code=response.status_code,
                body=response.text[:500],
            )

        try:
            return response.json()
        except ValueError as e:
            raise EmailSendError("Invalid JSON from receiving API") from e


# Module-level singleton (mirrors WhatsAppClient usage in main/jobs).
email_client = EmailClient()

"""
Email body helpers — merge fields, compliance footer, unsubscribe tokens.

Phase E2: used by POST /api/email/send.
Phase E3: verify_unsub_token powers GET /api/email/unsubscribe.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
from html import escape
from urllib.parse import quote

from app.core.config import EMAIL_UNSUB_SECRET, JWT_SECRET, PUBLIC_API_URL

# Default token lifetime: 90 days
_UNSUB_TTL_SECONDS = 90 * 24 * 3600

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email_format(email: str) -> bool:
    if not email or len(email) > 320:
        return False
    return bool(_EMAIL_RE.match(email.strip()))


def _unsub_secret() -> str:
    secret = (EMAIL_UNSUB_SECRET or JWT_SECRET or "").strip()
    if not secret:
        raise ValueError(
            "EMAIL_UNSUB_SECRET or JWT_SECRET must be set to sign unsubscribe links"
        )
    return secret


def make_unsub_token(client_id: int, email: str, ttl_seconds: int = _UNSUB_TTL_SECONDS) -> str:
    """
    Create a signed unsubscribe token: base64url(payload).base64url(sig).

    payload = "{client_id}|{email}|{exp_unix}"
    """
    email_norm = email.strip().lower()
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"{int(client_id)}|{email_norm}|{exp}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(
        _unsub_secret().encode("utf-8"),
        payload_b64.encode("ascii"),
        hashlib.sha256,
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{payload_b64}.{sig_b64}"


def verify_unsub_token(token: str) -> tuple[int, str] | None:
    """
    Validate token. Returns (client_id, email) or None if invalid/expired.
    Used by E3 unsubscribe endpoint; implemented now so E2 links are ready.
    """
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.rsplit(".", 1)
        # restore padding
        def _pad(s: str) -> str:
            return s + "=" * (-len(s) % 4)

        expected = hmac.new(
            _unsub_secret().encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        given = base64.urlsafe_b64decode(_pad(sig_b64))
        if not hmac.compare_digest(expected, given):
            return None
        payload = base64.urlsafe_b64decode(_pad(payload_b64)).decode("utf-8")
        parts = payload.split("|")
        if len(parts) != 3:
            return None
        client_id = int(parts[0])
        email = parts[1].strip().lower()
        exp = int(parts[2])
        if exp < int(time.time()):
            return None
        if not is_valid_email_format(email):
            return None
        return client_id, email
    except Exception:
        return None


def build_unsubscribe_url(client_id: int, email: str) -> str:
    """Absolute unsubscribe URL. Requires PUBLIC_API_URL."""
    base = (PUBLIC_API_URL or "").rstrip("/")
    if not base:
        raise ValueError("PUBLIC_API_URL must be set to build unsubscribe links")
    token = make_unsub_token(client_id, email)
    return f"{base}/api/email/unsubscribe?token={quote(token, safe='')}"


def apply_merge_fields(template: str, fields: dict[str, str | None]) -> str:
    """Replace {{key}} placeholders (case-sensitive keys as provided)."""
    if not template:
        return template
    out = template
    for key, value in fields.items():
        out = out.replace("{{" + key + "}}", value or "")
    return out


def build_compliance_footer_text(
    *,
    company_address: str | None,
    unsubscribe_url: str,
    custom_footer: str | None = None,
) -> str:
    lines = []
    if custom_footer and custom_footer.strip():
        # Strip tags for text version of custom HTML footer.
        plain = re.sub(r"<[^>]+>", "", custom_footer)
        lines.append(plain.strip())
    if company_address and company_address.strip():
        lines.append(company_address.strip())
    lines.append(f"Unsubscribe: {unsubscribe_url}")
    return "\n\n--\n" + "\n".join(lines)


def build_compliance_footer_html(
    *,
    company_address: str | None,
    unsubscribe_url: str,
    custom_footer_html: str | None = None,
) -> str:
    parts = ['<div style="margin-top:24px;padding-top:12px;border-top:1px solid #ddd;'
             'font-size:12px;color:#666;font-family:sans-serif;">']
    if custom_footer_html and custom_footer_html.strip():
        parts.append(custom_footer_html.strip())
    if company_address and company_address.strip():
        parts.append(f"<p>{escape(company_address.strip())}</p>")
    parts.append(
        f'<p><a href="{escape(unsubscribe_url, quote=True)}">Unsubscribe</a></p>'
    )
    parts.append("</div>")
    return "".join(parts)


def wrap_email_bodies(
    *,
    body_text: str,
    body_html: str | None,
    company_address: str | None,
    unsubscribe_url: str,
    custom_footer_html: str | None = None,
) -> tuple[str, str]:
    """
    Append compliance footer to text + HTML bodies.
    If body_html is omitted, build a simple HTML wrapper from text.
    """
    footer_text = build_compliance_footer_text(
        company_address=company_address,
        unsubscribe_url=unsubscribe_url,
        custom_footer=custom_footer_html,
    )
    final_text = (body_text or "").rstrip() + footer_text

    footer_html = build_compliance_footer_html(
        company_address=company_address,
        unsubscribe_url=unsubscribe_url,
        custom_footer_html=custom_footer_html,
    )
    if body_html and body_html.strip():
        final_html = body_html.rstrip() + footer_html
    else:
        # Minimal HTML from plain text (preserve newlines).
        escaped = escape(body_text or "").replace("\n", "<br>\n")
        final_html = (
            f'<div style="font-family:sans-serif;font-size:14px;color:#222;">'
            f"{escaped}</div>{footer_html}"
        )
    return final_text, final_html

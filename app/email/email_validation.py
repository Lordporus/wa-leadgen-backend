"""
Lead email validation + disposable-domain blocklist — Phase E4.

Format check reuses email_templates.is_valid_email_format.
DNS MX lookup is intentionally deferred (slow/unreliable on free hosts).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.email.email_templates import is_valid_email_format

# Common disposable / throwaway domains. Not exhaustive — blocks obvious junk
# on import/settings; real validation services can replace this later.
_DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.net",
        "guerrillamail.org",
        "sharklasers.com",
        "grr.la",
        "tempmail.com",
        "temp-mail.org",
        "temp-mail.io",
        "throwaway.email",
        "yopmail.com",
        "yopmail.fr",
        "trashmail.com",
        "trashmail.me",
        "trashmail.net",
        "10minutemail.com",
        "10minutemail.net",
        "minutemail.com",
        "getnada.com",
        "nada.email",
        "dispostable.com",
        "mailnesia.com",
        "maildrop.cc",
        "fakeinbox.com",
        "tempail.com",
        "emailondeck.com",
        "moakt.com",
        "discard.email",
        "discardmail.com",
        "spamgourmet.com",
        "mailcatch.com",
        "mytemp.email",
        "tmpmail.org",
        "tmpmail.net",
        "inboxkitten.com",
        "burnermail.io",
        "guerrillamailblock.com",
        "spam4.me",
        "mailnull.com",
        "jetable.org",
        "kasmail.com",
        "spamfree24.org",
        "mt2015.com",
        "trash-mail.com",
        "getairmail.com",
        "mailforspam.com",
        "trashymail.com",
        "mailin8r.com",
        "mailinater.com",
        "mailexpire.com",
        "tempinbox.com",
        "temporarily.de",
        "tmpeml.com",
        "emailfake.com",
        "crazymailing.com",
        "dropmail.me",
    }
)


@dataclass(frozen=True)
class EmailValidationResult:
    ok: bool
    email: str | None  # normalized lowercase when ok
    error: str | None = None
    disposable: bool = False


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def email_domain(email: str) -> str | None:
    email = normalize_email(email)
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[-1]


def is_disposable_domain(domain: str | None) -> bool:
    if not domain:
        return False
    d = domain.strip().lower().rstrip(".")
    if d in _DISPOSABLE_DOMAINS:
        return True
    # Subdomains of known disposables (e.g. foo.mailinator.com)
    for blocked in _DISPOSABLE_DOMAINS:
        if d.endswith("." + blocked):
            return True
    return False


def is_disposable_email(email: str) -> bool:
    return is_disposable_domain(email_domain(email))


def validate_lead_email(
    email: str | None,
    *,
    allow_empty: bool = False,
    block_disposable: bool = True,
) -> EmailValidationResult:
    """
    Validate an address for storage on a lead.

    allow_empty: treat blank as clear-email (ok, email=None).
    block_disposable: reject known throwaway domains when True.
    """
    if email is None or not str(email).strip():
        if allow_empty:
            return EmailValidationResult(ok=True, email=None, error=None)
        return EmailValidationResult(ok=False, email=None, error="email is required")

    raw = str(email).strip()
    if len(raw) > 320:
        return EmailValidationResult(
            ok=False, email=None, error="email exceeds 320 characters"
        )

    if not is_valid_email_format(raw):
        return EmailValidationResult(
            ok=False, email=None, error="email is not a valid address"
        )

    normalized = normalize_email(raw)
    # Reject consecutive dots / empty local part edge cases the loose regex allows weakly
    local, _, domain = normalized.partition("@")
    if not local or not domain or ".." in normalized or domain.startswith("."):
        return EmailValidationResult(
            ok=False, email=None, error="email is not a valid address"
        )

    disposable = is_disposable_domain(domain)
    if block_disposable and disposable:
        return EmailValidationResult(
            ok=False,
            email=None,
            error="disposable or throwaway email domains are not allowed",
            disposable=True,
        )

    return EmailValidationResult(
        ok=True, email=normalized, error=None, disposable=disposable
    )

"""
AI Safety Guardrails — 4-layer defense-in-depth stack.

Layer 1: Input Scanner     — block prompt injection before LLM call
Layer 2: Output Validation — empty/length check (done in Sprint 1 Task 6, see jobs.py)
Layer 3: Confidence Scorer — score LLM output 0.0–1.0, flag low-confidence replies
Layer 4: PII Guard         — redact Aadhaar, PAN, credit card numbers before LLM call

All layers are pure functions with no side effects. Wiring happens in jobs.py (Task 3).
"""

import re
import logging

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Input Scanner
# ─────────────────────────────────────────────────────────────────────────────

_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(your|previous|all|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+(your|previous|all|prior|above)\s+instructions", re.I),
    re.compile(r"forget\s+(your|previous|all)\s+instructions", re.I),
    re.compile(r"override\s+(your|previous|all)\s+instructions", re.I),
    re.compile(r"you\s+are\s+now\b", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"\bact\s+as\b", re.I),
    re.compile(r"pretend\s+(to\s+be|you\s+are)", re.I),
    re.compile(r"repeat\s+everything\s+above", re.I),
    re.compile(r"repeat\s+(your|the)\s+(instructions|system\s*prompt)", re.I),
    re.compile(r"what\s+(are|is)\s+(your|the)\s+(instructions|system\s*prompt)", re.I),
    re.compile(r"(show|reveal|print|output)\s+(your|the)\s+(system\s*prompt|instructions|config)", re.I),
    re.compile(r"do\s+not\s+follow\s+(your|any)\s+(rules|instructions)", re.I),
]

_SAFE_REFUSAL = (
    "I'm here to help you with our services! "
    "Kya main aapke liye kuch aur help kar sakta hu?"
)


def scan_input(user_text: str) -> tuple[bool, str | None]:
    """
    Check inbound message for prompt injection attempts.

    Returns:
        (True, None)              — input is safe, proceed to LLM
        (False, refusal_message)  — injection detected, send refusal instead
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(user_text):
            logger.warning(f"Prompt injection blocked: matched pattern '{pattern.pattern}'")
            return False, _SAFE_REFUSAL
    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: Output Validation
# ─────────────────────────────────────────────────────────────────────────────
# Already implemented in Sprint 1 Task 6 (empty check + 4096-char truncation).
# Lives inline in the webhook pipeline (jobs.py lines ~92-97). No code needed here.


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: Confidence Scorer
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_PATTERN = re.compile(
    r"\[(?:NAME|DATE|TIME|PHONE|EMAIL|ADDRESS|PLACEHOLDER|INSERT|TODO)\]"
    r"|"
    r"\{\{[^}]+\}\}",
    re.I,
)

_URL_PATTERN = re.compile(r"https?://[^\s,)\"']+", re.I)

CONFIDENCE_THRESHOLD = 0.6


def score_confidence(ai_reply: str, system_prompt: str | None = None) -> float:
    """
    Score an LLM response 0.0–1.0.

    Penalties:
      - Too short (< 15 chars)                    → −0.4
      - Contains unresolved placeholders           → −0.3 per match (max −0.6)
      - Contains URLs not present in system prompt → −0.3 per URL   (max −0.6)

    Returns clamped float in [0.0, 1.0].
    """
    score = 1.0

    stripped = ai_reply.strip()
    if len(stripped) < 15:
        score -= 0.4

    placeholders = _PLACEHOLDER_PATTERN.findall(stripped)
    if placeholders:
        score -= min(len(placeholders) * 0.3, 0.6)

    urls_in_reply = _URL_PATTERN.findall(stripped)
    if urls_in_reply:
        allowed_urls = set()
        if system_prompt:
            allowed_urls = set(_URL_PATTERN.findall(system_prompt))

        rogue_urls = [u for u in urls_in_reply if u not in allowed_urls]
        if rogue_urls:
            score -= min(len(rogue_urls) * 0.3, 0.6)

    return max(0.0, min(1.0, score))


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: PII Guard
# ─────────────────────────────────────────────────────────────────────────────

_AADHAAR_PATTERN = re.compile(
    r"\b[2-9]\d{3}[\s\-]?\d{4}[\s\-]?\d{4}\b"
)

_PAN_PATTERN = re.compile(
    r"\b[A-Z]{5}\d{4}[A-Z]\b"
)

_CREDIT_CARD_PATTERN = re.compile(
    r"\b(?:\d[\s\-]?){13,19}\b"
)

_REDACTED = "[REDACTED]"


def redact_pii(text: str) -> str:
    """
    Replace Aadhaar numbers, PAN numbers, and credit card numbers
    with [REDACTED] before passing text to the LLM.

    Returns the sanitised text.
    """
    result = text
    result = _AADHAAR_PATTERN.sub(_REDACTED, result)
    result = _PAN_PATTERN.sub(_REDACTED, result)
    result = _CREDIT_CARD_PATTERN.sub(_REDACTED, result)
    return result

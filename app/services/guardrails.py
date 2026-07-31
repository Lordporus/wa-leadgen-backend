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
    re.compile(r"ignore\s+all\s+previous\s+instructions", re.I),
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

_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\s().-]?){7,15}(?!\w)")
_ACCOUNT_PATTERN = re.compile(
    r"\b(?:(?:account|customer|client|policy|order|invoice|booking)\s*(?:id|identifier|number|no\.?|#)|(?:payment|transaction|wallet)\s*(?:id|reference|ref\.?|number|no\.?|#))\s*(?:(?:is|equals?)\s*)?[:=-]?\s*[A-Z0-9][A-Z0-9-]{3,}\b",
    re.I,
)
_ADDRESS_PATTERN = re.compile(
    r"\b\d{1,6}\s+[A-Z0-9 .'-]{2,60}\s+(?:road|rd|street|st|avenue|ave|lane|ln|sector|nagar|colony|building|floor)\b[^,\n]*",
    re.I,
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
    result = _EMAIL_PATTERN.sub(_REDACTED, result)
    result = _PHONE_PATTERN.sub(_REDACTED, result)
    result = _ACCOUNT_PATTERN.sub(_REDACTED, result)
    result = _ADDRESS_PATTERN.sub(_REDACTED, result)
    return result


def minimize_sensitive_text(text: str, *, known_names: list[str] | None = None) -> str:
    """Content minimisation used before prompts, summaries, or logs."""
    result = redact_pii(text)
    for name in known_names or []:
        candidate = name.strip()
        if len(candidate) >= 3:
            result = re.sub(rf"\b{re.escape(candidate)}\b", "[REDACTED]", result, flags=re.I)
    return result


_COMMITMENT_PATTERN = re.compile(
    r"\b(guarantee(?:d)?|legally binding|legal advice|contract(?:ual)?|diagnos\w*|prescri\w*|medical advice|financial advice|invest\w*|return on investment|100%|refund|definitely (?:increase|double|save)|will (?:increase|double|save))\b",
    re.I,
)
_SENSITIVE_PATTERN = re.compile(r"\b(password|otp|cvv|secret|api[_ -]?key)\b", re.I)
_NUMBER_OR_PERCENT = re.compile(r"(?:₹|\$|\brs\.?)?\s*\d[\d,]*(?:\.\d+)?%?", re.I)
_PROFANITY = re.compile(r"\b(?:idiot|stupid|damn|shut up|hate you)\b", re.I)
_INSULTING_TONE = re.compile(
    r"\b(?:ridiculous|incompetent|clueless|foolish|ignorant|absurd|moron|nonsense|pathetic|worthless|annoying|your fault|waste of time|clearly do not understand)\b",
    re.I,
)
_DISRESPECTFUL_STRUCTURE = re.compile(
    r"\b(?:you\s+are|are\s+you)\s+(?!(?:available|interested|ready|comfortable|able|welcome)\b)[a-z]+|\b(?:you|your)\b.{0,35}\b(?:wrong|failed|failure|fault|no\b|nothing|anything|little|do not understand|cannot understand)\b",
    re.I,
)
_SAFE_CONVERSATIONAL = re.compile(
    r"^(?:how\s+can\s+we\s+help(?:\s+you)?(?:\s+with\s+your\s+request)?|(?:please|kindly)\s+(?:tell|share|provide|confirm|choose|select)\s+(?:us\s+)?your\s+(?:preferred\s+)?(?:booking\s+)?(?:time|date|availability|preference|requirements?|request)|(?:can|could|would)\s+you\s+(?:tell|share|provide|confirm|choose|select)\s+(?:us\s+)?your\s+(?:preferred\s+)?(?:booking\s+)?(?:time|date|availability|preference|requirements?|request)|(?:what|when)\s+(?:(?:is|are|would|can)\s+)?(?:your|you)\s+(?:preferred\s+)?(?:booking\s+)?(?:time|date|availability|preference)|do\s+you\s+want\s+(?:help|to\s+proceed)|would\s+you\s+like\s+(?:help|to\s+proceed)|are\s+you\s+(?:available|interested|ready|comfortable)(?:\s+(?:now|today|tomorrow))?)\s*[.!?]?$",
    re.I,
)
_FACTUAL_CONTENT = re.compile(
    r"\d|(?:₹|\$|\brs\.?)|\b(?:we|our|company|business|service|product|team|office|location|customers?|clients?)\b.{0,80}\b(?:is|are|have|can|will|serve|provide|offer|support|operate|deliver|achieve|speciali[sz]e|locate|cover|include)\w*\b|\b(?:serve|provide|offer|support|operate|deliver|achieve|speciali[sz]e|locate|cover|include)\w*\b",
    re.I,
)
_ENGLISH_FACTUAL_QUESTION = re.compile(
    r"^(?:(?:are|is)\s+(?:we|it|this|that|our\s+\w+|the\s+\w+)\s+(?:available|located|open|active|approved|able|in\b|at\b|the\b|a\b|an\b).+|(?:do|does|can|will)\s+(?:we|our\s+\w+|the\s+\w+)\s+(?:serve|provide|offer|support|operate|deliver|achieve|speciali[sz]e|cover|include)\w*\b.+)\?$",
    re.I,
)
_ENGLISH_STATEMENT_FORM = re.compile(
    r"^(?:(?:please|kindly)\s+(?:tell|share|provide|confirm|choose|select)\b|(?:i|we|you|it|this|that|there)\s+(?:am|are|is|was|were|have|has|can|could|will|would|should|do|did)\b|(?:our|your|the|a|an)\b.+\b(?:is|are|was|were|has|have|can|could|will|would|should|includes?|provides?|offers?|serves?)\b|[a-z][a-z '-]{1,80}\s+(?:is|are|was|were|has|have|can|could|will|would|should|includes?|provides?|offers?|serves?)\b).*[.!]?$",
    re.I,
)
_INCOHERENT_ORDER = re.compile(
    r"\b(?:we|you|it|this|that)\s+(?:the|a|an)\b|\b(?:the|a|an)\s+(?:we|you|it|this|that)\b|\b(?:the|a|an|our|your)\s+(?:is|are|was|were|has|have)\b|\b(?:the|a|an|our|your)\s*[.!?]?$",
    re.I,
)
_MISORDERED_LOCATION = re.compile(
    r"\b(?:is|are|was|were)\s+[A-Z][a-z]+\s+(?:available|located|open)\b|\b(?:available|located|open)\s+[A-Z][a-z]+\b"
)
_SAFE_PREFIX = re.compile(r"^(?:thank you|thanks|sorry|i understand|we understand)\s*[,;:!.-]*\s*", re.I)
_ENGLISH_FUNCTION_WORDS = frozenset(
    "a an and are as at be can could did do does for from has have how i in is it of on or our please should that the this to us was we were what when where why will with would you your".split()
)
_UNSUPPORTED_LANGUAGE_MARKERS = re.compile(
    r"\b(?:bonjour|merci|pour|votre|demande|hola|gracias|guten|danke|bitte|ciao|grazie|prego)\b",
    re.I,
)


def _normalise_claim(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9₹$%]+", value.lower()))


def _is_safe_conversational(sentence: str) -> bool:
    return bool(
        _SAFE_CONVERSATIONAL.fullmatch(sentence)
        and not _FACTUAL_CONTENT.search(sentence)
    )


def _claims_are_grounded(reply: str, grounded_chunks: list[str]) -> bool:
    # Only complete, narrowly approved lead-solicitation forms are non-claims.
    # Every other sentence, including every other question, requires evidence.
    claims = []
    for match in re.finditer(r"[^.!?\n]+[.!?]?", reply):
        sentence = match.group(0).strip()
        if not sentence:
            continue
        sentence = _SAFE_PREFIX.sub("", sentence).strip()
        if not sentence:
            continue
        if _is_safe_conversational(sentence):
            continue
        claims.append(sentence.rstrip(".!?"))
    if not claims:
        return True
    normalised_chunks = [_normalise_claim(chunk) for chunk in grounded_chunks]
    for claim in claims:
        normalised = _normalise_claim(claim)
        exact_values = {_normalise_claim(value) for value in _NUMBER_OR_PERCENT.findall(claim)}
        if not any(
            normalised and normalised in chunk
            and all(value in chunk for value in exact_values)
            for chunk in normalised_chunks
        ):
            return False
    return True


def _language_allowed(reply: str, allowed_languages: list[str], grounded_chunks: list[str]) -> bool:
    aliases = {"english": "en", "hindi": "hi"}
    allowed = {aliases.get(value.strip().lower(), value.strip().lower()) for value in allowed_languages}
    if not allowed:
        return False
    has_devanagari = bool(re.search(r"[\u0900-\u097f]", reply))
    hinglish = bool(re.search(r"\b(?:aap|hai|hain|kya|ke liye|kar sakte|ji)\b", reply, re.I))
    has_latin = bool(re.search(r"[A-Za-z]", reply))
    if has_devanagari:
        detected = "hinglish" if has_latin else "hi"
    elif hinglish:
        detected = "hinglish"
    elif _UNSUPPORTED_LANGUAGE_MARKERS.search(reply) or re.search(r"[^\x00-\x7f₹]", reply):
        return False
    else:
        del grounded_chunks  # Coherence is determined independently of vocabulary provenance.
        reply_words = re.findall(r"[a-z]+", reply.lower())
        if not reply_words:
            return False
        function_ratio = sum(word in _ENGLISH_FUNCTION_WORDS for word in reply_words) / len(reply_words)
        if function_ratio < 0.25:
            return False
        for raw_sentence in re.findall(r"[^.!?\n]+[.!?]?", reply):
            sentence = raw_sentence.strip()
            if not sentence:
                continue
            sentence = _SAFE_PREFIX.sub("", sentence).strip()
            if not sentence or _is_safe_conversational(sentence):
                continue
            if _INCOHERENT_ORDER.search(sentence) or _MISORDERED_LOCATION.search(sentence):
                return False
            grammar = _ENGLISH_FACTUAL_QUESTION if sentence.endswith("?") else _ENGLISH_STATEMENT_FORM
            if not grammar.fullmatch(sentence):
                return False
        detected = "en"
    return detected in allowed


def _tone_allowed(reply: str, tone: str) -> bool:
    if tone not in {"professional", "professional_concise", "friendly_professional"}:
        return False
    if _PROFANITY.search(reply) or _INSULTING_TONE.search(reply) or _DISRESPECTFUL_STRUCTURE.search(reply) or reply.count("!") > 2:
        return False
    for raw_sentence in re.findall(r"[^.!?\n]+[.!?]?", reply):
        sentence = _SAFE_PREFIX.sub("", raw_sentence.strip()).strip()
        if not sentence or not re.search(r"\b(?:you|your)\b", sentence, re.I):
            continue
        if _is_safe_conversational(sentence):
            continue
        if "?" in sentence or re.match(r"^(?:you|your)\b", sentence, re.I) or re.search(
            r"\byou\s+(?:are|have|know|understand|seem|sound)\b|\byour\b.{0,40}\b(?:is|are|seems?|sounds?)\b",
            sentence,
            re.I,
        ):
            return False
    letters = [char for char in reply if char.isalpha()]
    if letters and sum(char.isupper() for char in letters) / len(letters) > 0.6:
        return False
    return tone != "professional_concise" or len(reply) <= 600


def validate_ai_response(
    reply: str,
    prompt: str,
    grounded_chunks: list[str],
    *,
    allowed_languages: list[str],
    tone: str,
) -> dict[str, object]:
    """Deterministic send gate; any unsupported or unsafe reply fails closed."""
    reason = None
    score = score_confidence(reply, prompt)
    if not reply or len(reply) > 4096:
        reason = "empty_or_oversize"
    elif _COMMITMENT_PATTERN.search(reply):
        reason = "prohibited_commitment"
    elif _SENSITIVE_PATTERN.search(reply) or any(
        pattern.search(reply)
        for pattern in (
            _AADHAAR_PATTERN,
            _PAN_PATTERN,
            _CREDIT_CARD_PATTERN,
            _EMAIL_PATTERN,
            _PHONE_PATTERN,
            _ACCOUNT_PATTERN,
            _ADDRESS_PATTERN,
        )
    ):
        reason = "sensitive_data_exposure"
    elif _PLACEHOLDER_PATTERN.search(reply):
        reason = "unresolved_placeholder"
    elif _URL_PATTERN.search(reply) and not all(url in prompt or any(url in chunk for chunk in grounded_chunks) for url in _URL_PATTERN.findall(reply)):
        reason = "unsupported_url"
    elif not _tone_allowed(reply, tone):
        reason = "tone_not_allowed"
    elif not _language_allowed(reply, allowed_languages, [prompt, *grounded_chunks]):
        reason = "language_not_allowed"
    elif not _claims_are_grounded(reply, [prompt, *grounded_chunks]):
        reason = "unsupported_claim"
    return {"allowed": reason is None and score >= CONFIDENCE_THRESHOLD, "reason": reason or ("low_output_confidence" if score < CONFIDENCE_THRESHOLD else "allowed"), "output_confidence": score}

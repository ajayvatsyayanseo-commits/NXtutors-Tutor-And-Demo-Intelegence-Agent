"""PII detection, redaction and pseudonymisation.

Three separate jobs, deliberately not conflated:

* `redact()` — strip identifiers from text **before it leaves the process**
  (to an LLM, a log line, a memory event). Destructive and irreversible.
* `pseudonym()` — a stable, non-reversible handle for an identifier, so logs and
  traces can correlate turns without ever holding the identifier.
* `mask()` — a human-readable partial for a support console (`+91•••••3210`).

The pepper is a secret. A plain SHA-256 of a 10-digit Indian phone number is
trivially brute-forced — the entire keyspace is 10 billion, minutes of GPU time.
Peppered, it is not.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Final

#: Indian mobile numbers. Separators may appear *between* digits — `98765 43210`
#: and `987-654-3210` are both routine, and a pattern that only matches the
#: unseparated form silently leaks the most common way people type their number.
#: Lookarounds stop it matching a slice out of a longer digit run.
_PHONE: Final = re.compile(r"(?<!\d)(?:\+?91[\s.\-]?)?[6-9](?:[\s.\-]?\d){9}(?!\d)")
_EMAIL: Final = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
#: 12-digit Aadhaar, usually grouped 4-4-4.
_AADHAAR: Final = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_PAN: Final = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
#: 6-digit pincode. Redacted from LLM payloads but kept for matching internally.
_PINCODE: Final = re.compile(r"\b[1-9]\d{5}\b")
_URL: Final = re.compile(r"https?://\S+")
#: Long digit runs that are probably an account/card number.
_LONG_DIGITS: Final = re.compile(r"\b\d{12,19}\b")

# Replacement placeholders, not credentials. S105 pattern-matches the names.
PHONE_TOKEN = "[phone]"  # noqa: S105
EMAIL_TOKEN = "[email]"  # noqa: S105
ID_TOKEN = "[id]"  # noqa: S105
URL_TOKEN = "[url]"  # noqa: S105
PINCODE_TOKEN = "[pincode]"  # noqa: S105

#: Ordered: Aadhaar before phone, or a 12-digit Aadhaar's tail matches as a
#: phone number and only half of it gets redacted.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_EMAIL, EMAIL_TOKEN),
    (_URL, URL_TOKEN),
    (_AADHAAR, ID_TOKEN),
    (_LONG_DIGITS, ID_TOKEN),
    (_PAN, ID_TOKEN),
    (_PHONE, PHONE_TOKEN),
)


def redact(text: str | None, *, redact_pincode: bool = False) -> str:
    """Remove direct identifiers from free text.

    `redact_pincode` is off by default because a pincode is load-bearing for
    matching and is not by itself identifying. It is switched **on** for LLM
    payloads, where pincode plus locality plus class narrows to a small enough
    group to be worth withholding.
    """
    if not text:
        return ""
    out = text
    for pattern, token in _RULES:
        out = pattern.sub(token, out)
    if redact_pincode:
        out = _PINCODE.sub(PINCODE_TOKEN, out)
    return out


def contains_pii(text: str | None) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern, _ in _RULES)


def found_pii_kinds(text: str | None) -> list[str]:
    """Which categories are present. For metrics — never logs the values."""
    if not text:
        return []
    kinds = {
        "email": _EMAIL,
        "url": _URL,
        "aadhaar": _AADHAAR,
        "pan": _PAN,
        "phone": _PHONE,
    }
    return sorted(name for name, pattern in kinds.items() if pattern.search(text))


class Pseudonymiser:
    """Stable, non-reversible handles for identifiers.

    Same input plus same pepper gives the same handle forever, so a conversation
    can be followed across logs and traces; without the pepper the handle cannot
    be reversed even for a small keyspace like phone numbers.
    """

    def __init__(self, pepper: str) -> None:
        if not pepper:
            raise ValueError("pseudonymiser requires a non-empty pepper")
        self._pepper = pepper.encode("utf-8")

    def hash(self, value: str, *, length: int = 16) -> str:
        digest = hmac.new(self._pepper, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:length]

    def conversation(self, conversation_id: str) -> str:
        return f"cv_{self.hash(conversation_id)}"

    def phone(self, phone: str) -> str:
        return f"ph_{self.hash(_normalise_phone(phone))}"

    def parent(self, identifier: str) -> str:
        return f"pt_{self.hash(identifier)}"


def _normalise_phone(phone: str) -> str:
    """Strip formatting so `+91 98765 43210` and `9876543210` hash identically."""
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def mask_phone(phone: str | None) -> str:
    """`+91•••••3210` — enough for a human to confirm, useless to an attacker."""
    if not phone:
        return ""
    digits = _normalise_phone(phone)
    if len(digits) < 4:
        return "•" * len(digits)
    return f"•••••{digits[-4:]}"


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    head = local[0] if local else ""
    return f"{head}•••@{domain}"


#: Metric label values must be low-cardinality AND non-identifying. A phone hash
#: is non-identifying but high-cardinality; it would blow up CloudWatch costs and
#: is banned from labels regardless.
FORBIDDEN_IN_METRIC_LABELS: frozenset[str] = frozenset(
    {
        "phone",
        "phone_hash",
        "email",
        "conversation_id",
        "parent_ref",
        "student_ref",
        "name",
        "address",
        "message",
        "text",
    }
)


def assert_label_safe(labels: dict[str, str]) -> None:
    """Raise if a metric label carries an identifier. Called by the metrics layer."""
    offenders = sorted(set(labels) & FORBIDDEN_IN_METRIC_LABELS)
    if offenders:
        raise ValueError(f"identifying metric labels are not allowed: {offenders}")

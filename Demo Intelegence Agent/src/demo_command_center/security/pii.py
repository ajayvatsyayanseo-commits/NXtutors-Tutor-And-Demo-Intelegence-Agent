"""PII redaction, pseudonymisation and masking.

Three separate jobs, deliberately not conflated:

* `redact()` — strip identifiers from text **before it leaves the process** to
  an LLM prompt, a log line or an alert body. Destructive and irreversible.
* `Pseudonymiser` — a stable, non-reversible handle so logs and traces can
  correlate a conversation without ever holding the phone number.
* `mask_*()` — a human-readable partial for the ops console (`•••••3210`).

The pepper is a secret and the reason is arithmetic: a plain SHA-256 of a
10-digit Indian mobile has a keyspace of 10 billion, which is minutes of GPU
time. Peppered with a secret held in Secrets Manager, it is not reversible.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Final

#: Indian mobile numbers, tolerating the separators people actually type.
_PHONE: Final = re.compile(r"(?<!\d)(?:\+?91[\s.\-]?)?[6-9](?:[\s.\-]?\d){9}(?!\d)")
_EMAIL: Final = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")
_AADHAAR: Final = re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b")
_PAN: Final = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_PINCODE: Final = re.compile(r"\b[1-9]\d{5}\b")
_URL: Final = re.compile(r"https?://\S+")
_LONG_DIGITS: Final = re.compile(r"\b\d{12,19}\b")
#: UPI VPAs turn up when a parent types their payment handle into WhatsApp.
_UPI: Final = re.compile(r"\b[\w.\-]{2,}@(?:ok\w+|paytm|ybl|upi|axl|ibl|apl)\b", re.IGNORECASE)

# Replacement placeholders, not credentials. Ruff's S105 matches the names.
PHONE_TOKEN = "[phone]"  # noqa: S105
EMAIL_TOKEN = "[email]"  # noqa: S105
ID_TOKEN = "[id]"  # noqa: S105
URL_TOKEN = "[url]"  # noqa: S105
PINCODE_TOKEN = "[pincode]"  # noqa: S105
UPI_TOKEN = "[upi]"  # noqa: S105

#: Order matters. Aadhaar before phone, or the tail of a 12-digit Aadhaar
#: matches as a phone number and only half of it gets redacted. UPI before
#: email, or `name@okaxis` is consumed by the email rule first.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_UPI, UPI_TOKEN),
    (_EMAIL, EMAIL_TOKEN),
    (_URL, URL_TOKEN),
    (_AADHAAR, ID_TOKEN),
    (_LONG_DIGITS, ID_TOKEN),
    (_PAN, ID_TOKEN),
    (_PHONE, PHONE_TOKEN),
)


def redact(text: str | None, *, redact_pincode: bool = False) -> str:
    """Remove direct identifiers from free text.

    `redact_pincode` defaults off because a pincode is load-bearing for regional
    routing and is not identifying on its own. It is switched **on** for LLM
    payloads, where pincode plus class plus subject narrows to a small enough
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
        "upi": _UPI,
    }
    return sorted(name for name, pattern in kinds.items() if pattern.search(text))


class Pseudonymiser:
    """Stable, non-reversible handles for identifiers."""

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
        """Normalised first, so `+91 98765 43210` and `9876543210` collide by
        design — they are the same person and must land in the same lane."""
        return f"ph_{self.hash(normalise_phone(phone))}"

    def contact(self, identifier: str) -> str:
        return f"ct_{self.hash(identifier)}"


def normalise_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def mask_phone(phone: str | None) -> str:
    """`•••••3210` — enough for a human to confirm, useless to an attacker."""
    if not phone:
        return ""
    digits = normalise_phone(phone)
    if len(digits) < 4:
        return "•" * len(digits)
    return f"•••••{digits[-4:]}"


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    return f"{local[:1]}•••@{domain}"


#: Metric label values must be low-cardinality AND non-identifying. A phone hash
#: is non-identifying but high-cardinality; it would multiply CloudWatch custom
#: metric cost by the user count, so it is banned from labels regardless.
FORBIDDEN_IN_METRIC_LABELS: frozenset[str] = frozenset(
    {
        "phone",
        "phone_hash",
        "email",
        "conversation_id",
        "student_ref",
        "tutor_ref",
        "recipient_ref",
        "name",
        "address",
        "message",
        "text",
        "meet_url",
        "order_ref",
    }
)


def assert_label_safe(labels: dict[str, str]) -> None:
    """Raise if a metric label carries an identifier. Called by the metrics layer."""
    offenders = sorted(set(labels) & FORBIDDEN_IN_METRIC_LABELS)
    if offenders:
        raise ValueError(f"identifying metric labels are not allowed: {offenders}")

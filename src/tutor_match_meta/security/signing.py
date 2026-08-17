"""Request signing, replay protection and idempotency keys.

Every inbound call from another NXTutors agent is HMAC-SHA256 signed over a
canonical string that binds the method, path, timestamp and body hash together.
Signing only the body would let an attacker replay a valid body against a
different endpoint.

Replay protection is two-layer: a timestamp window (cheap, stateless, catches
the bulk) and a durable idempotency record keyed on the dedup key (authoritative,
catches a replay inside the window). The window alone is not enough — Meta
genuinely redelivers webhooks within seconds.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import StrEnum

SIGNATURE_HEADER = "X-Nxt-Signature"
TIMESTAMP_HEADER = "X-Nxt-Timestamp"
AGENT_HEADER = "X-Nxt-Agent"
TRACE_HEADER = "X-Trace-Id"
SIGNATURE_PREFIX = "v1="


class VerificationFailure(StrEnum):
    MISSING_SIGNATURE = "missing_signature"
    MISSING_TIMESTAMP = "missing_timestamp"
    MALFORMED_TIMESTAMP = "malformed_timestamp"
    TIMESTAMP_OUT_OF_WINDOW = "timestamp_out_of_window"
    MALFORMED_SIGNATURE = "malformed_signature"
    SIGNATURE_MISMATCH = "signature_mismatch"
    BODY_TOO_LARGE = "body_too_large"


class SignatureError(Exception):
    def __init__(self, reason: VerificationFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SignedRequest:
    method: str
    path: str
    timestamp: int
    body: bytes


def canonical_string(method: str, path: str, timestamp: int, body: bytes) -> str:
    """The exact bytes that get signed.

    Binding method and path prevents a valid signature for `/ingress/lead` being
    replayed against `/ingress/selection`. The body is hashed rather than
    embedded so the canonical string stays a fixed size.
    """
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}"


def sign(secret: str, request: SignedRequest) -> str:
    """Produce the `X-Nxt-Signature` value."""
    if not secret:
        raise ValueError("signing secret is empty")
    payload = canonical_string(request.method, request.path, request.timestamp, request.body)
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify(
    secret: str,
    request: SignedRequest,
    provided_signature: str | None,
    *,
    tolerance_seconds: int,
    max_body_bytes: int,
    now: float | None = None,
) -> None:
    """Verify a signature or raise `SignatureError`.

    Checks are ordered cheapest-first so an unauthenticated flood is rejected
    before any HMAC work happens.
    """
    if len(request.body) > max_body_bytes:
        raise SignatureError(VerificationFailure.BODY_TOO_LARGE)
    if not provided_signature:
        raise SignatureError(VerificationFailure.MISSING_SIGNATURE)
    if not provided_signature.startswith(SIGNATURE_PREFIX):
        raise SignatureError(VerificationFailure.MALFORMED_SIGNATURE)

    current = time.time() if now is None else now
    drift = abs(current - request.timestamp)
    if drift > tolerance_seconds:
        raise SignatureError(VerificationFailure.TIMESTAMP_OUT_OF_WINDOW)

    expected = sign(secret, request)
    # Constant-time: a fast-fail comparison leaks the signature byte by byte.
    if not hmac.compare_digest(expected, provided_signature):
        raise SignatureError(VerificationFailure.SIGNATURE_MISMATCH)


def parse_timestamp(raw: str | None) -> int:
    if raw is None:
        raise SignatureError(VerificationFailure.MISSING_TIMESTAMP)
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise SignatureError(VerificationFailure.MALFORMED_TIMESTAMP) from exc


def idempotency_key(*parts: str | None) -> str:
    """A stable dedup key from the parts that make an event unique.

    Hashed rather than concatenated so the key is a fixed length regardless of
    how long a provider message id is, and so it never itself contains PII.
    """
    material = "\x1f".join(part or "" for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]

"""Inbound signature verification for the three webhook sources.

Three providers, three different schemes, one rule: **verify over the raw bytes
that arrived**. Re-serialising the parsed JSON and signing that is the classic
way to make signature verification pass for a payload that is not the payload
that was signed — key order, unicode escaping and float formatting all differ.
Every function here takes `bytes`, never a dict.

* Meta — `X-Hub-Signature-256: sha256=<hmac>` over the body with the app secret.
* Cashfree — base64 HMAC-SHA256 over `timestamp + raw_body` with the secret key.
* NXTutors agents — our own scheme, binding method and path as well as the body
  so a valid signature cannot be replayed against a different endpoint.

Every comparison is `hmac.compare_digest`. A `==` on a hex digest leaks the
correct prefix through timing and is a genuine remote attack on a signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from enum import StrEnum

META_SIGNATURE_HEADER = "x-hub-signature-256"
META_SIGNATURE_PREFIX = "sha256="
CASHFREE_SIGNATURE_HEADER = "x-webhook-signature"
CASHFREE_TIMESTAMP_HEADER = "x-webhook-timestamp"
INTERNAL_SIGNATURE_HEADER = "x-nxt-signature"
INTERNAL_TIMESTAMP_HEADER = "x-nxt-timestamp"
INTERNAL_SIGNATURE_PREFIX = "v1="


class SignatureFailure(StrEnum):
    MISSING_SIGNATURE = "missing_signature"
    MALFORMED_SIGNATURE = "malformed_signature"
    MISSING_TIMESTAMP = "missing_timestamp"
    MALFORMED_TIMESTAMP = "malformed_timestamp"
    TIMESTAMP_OUT_OF_WINDOW = "timestamp_out_of_window"
    SIGNATURE_MISMATCH = "signature_mismatch"
    BODY_TOO_LARGE = "body_too_large"
    # A failure-reason label, not a credential. S105 matches on the name.
    EMPTY_SECRET = "empty_secret"  # noqa: S105


class SignatureError(Exception):
    """Rejected at the edge. The reason is a metric label, never a user message."""

    def __init__(self, reason: SignatureFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _require_secret(secret: str) -> bytes:
    if not secret or not secret.strip():
        raise SignatureError(SignatureFailure.EMPTY_SECRET)
    return secret.encode("utf-8")


def _check_size(body: bytes, max_body_bytes: int) -> None:
    # First check on every path: an oversized body is rejected before any hashing
    # work, so a flood of 10 MB posts costs almost nothing.
    if len(body) > max_body_bytes:
        raise SignatureError(SignatureFailure.BODY_TOO_LARGE)


# --------------------------------------------------------------------- Meta


def meta_signature(app_secret: str, raw_body: bytes) -> str:
    key = _require_secret(app_secret)
    digest = hmac.new(key, raw_body, hashlib.sha256).hexdigest()
    return f"{META_SIGNATURE_PREFIX}{digest}"


def verify_meta(
    *, app_secret: str, raw_body: bytes, provided: str | None, max_body_bytes: int
) -> None:
    """Verify `X-Hub-Signature-256`, or raise.

    Meta has no timestamp header, so there is no replay window to check here —
    replay is caught by the durable `inbound_webhook_events` dedup on the Meta
    message id, which is the authoritative layer anyway.
    """
    _check_size(raw_body, max_body_bytes)
    if not provided:
        raise SignatureError(SignatureFailure.MISSING_SIGNATURE)
    if not provided.startswith(META_SIGNATURE_PREFIX):
        raise SignatureError(SignatureFailure.MALFORMED_SIGNATURE)
    if not hmac.compare_digest(meta_signature(app_secret, raw_body), provided):
        raise SignatureError(SignatureFailure.SIGNATURE_MISMATCH)


def verify_meta_challenge(*, verify_token: str, mode: str | None, token: str | None) -> bool:
    """Meta's GET verification handshake.

    Constant-time on the token too: this endpoint is unauthenticated and public,
    so a timing oracle here hands over the token that lets someone else register
    their own webhook receiver.
    """
    if mode != "subscribe" or token is None:
        return False
    return hmac.compare_digest(verify_token, token)


# ----------------------------------------------------------------- Cashfree


def cashfree_signature(secret_key: str, *, timestamp: str, raw_body: bytes) -> str:
    """Cashfree PG signs `timestamp + rawBody` and base64-encodes the digest."""
    key = _require_secret(secret_key)
    payload = timestamp.encode("utf-8") + raw_body
    return base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode("ascii")


def verify_cashfree(
    *,
    secret_key: str,
    raw_body: bytes,
    timestamp: str | None,
    provided: str | None,
    max_body_bytes: int,
    tolerance_seconds: int,
    now: float | None = None,
) -> None:
    """Verify a Cashfree webhook, or raise.

    Cashfree *does* send a timestamp and includes it in the signed material, so
    the replay window is enforced here as well as by the durable
    `payment_events` unique constraint. Two layers on the money path is
    deliberate: the cheap one sheds load, the durable one is authoritative.
    """
    _check_size(raw_body, max_body_bytes)
    if not provided:
        raise SignatureError(SignatureFailure.MISSING_SIGNATURE)
    if timestamp is None:
        raise SignatureError(SignatureFailure.MISSING_TIMESTAMP)
    try:
        sent_at = float(timestamp)
    except (TypeError, ValueError) as exc:
        raise SignatureError(SignatureFailure.MALFORMED_TIMESTAMP) from exc

    # Cashfree sends epoch seconds; be tolerant of a milliseconds variant rather
    # than rejecting every webhook if they change the unit.
    if sent_at > 1e11:
        sent_at /= 1000.0
    current = time.time() if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        raise SignatureError(SignatureFailure.TIMESTAMP_OUT_OF_WINDOW)

    expected = cashfree_signature(secret_key, timestamp=timestamp, raw_body=raw_body)
    if not hmac.compare_digest(expected, provided):
        raise SignatureError(SignatureFailure.SIGNATURE_MISMATCH)


# ------------------------------------------------------- NXTutors internal


@dataclass(frozen=True, slots=True)
class SignedRequest:
    method: str
    path: str
    timestamp: int
    body: bytes


def internal_canonical_string(method: str, path: str, timestamp: int, body: bytes) -> str:
    """Method + path + timestamp + body hash, newline separated.

    The path is in there so a signature minted for `/internal/handoff` cannot be
    replayed against `/ops/demos`, which would otherwise be a straight
    authorization bypass between two endpoints that share a signing key.
    """
    return f"{method.upper()}\n{path}\n{timestamp}\n{hashlib.sha256(body).hexdigest()}"


def sign_internal(secret: str, request: SignedRequest) -> str:
    key = _require_secret(secret)
    payload = internal_canonical_string(
        request.method, request.path, request.timestamp, request.body
    )
    digest = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{INTERNAL_SIGNATURE_PREFIX}{digest}"


def verify_internal(
    *,
    secret: str,
    request: SignedRequest,
    provided: str | None,
    tolerance_seconds: int,
    max_body_bytes: int,
    now: float | None = None,
) -> None:
    _check_size(request.body, max_body_bytes)
    if not provided:
        raise SignatureError(SignatureFailure.MISSING_SIGNATURE)
    if not provided.startswith(INTERNAL_SIGNATURE_PREFIX):
        raise SignatureError(SignatureFailure.MALFORMED_SIGNATURE)
    current = time.time() if now is None else now
    if abs(current - request.timestamp) > tolerance_seconds:
        raise SignatureError(SignatureFailure.TIMESTAMP_OUT_OF_WINDOW)
    if not hmac.compare_digest(sign_internal(secret, request), provided):
        raise SignatureError(SignatureFailure.SIGNATURE_MISMATCH)


def parse_timestamp(raw: str | None) -> int:
    if raw is None:
        raise SignatureError(SignatureFailure.MISSING_TIMESTAMP)
    try:
        return int(float(raw))
    except (TypeError, ValueError) as exc:
        raise SignatureError(SignatureFailure.MALFORMED_TIMESTAMP) from exc


def idempotency_key(*parts: str | None) -> str:
    """A fixed-length dedup key from the parts that make an event unique.

    Hashed rather than concatenated so the key never itself contains PII (a
    phone number is a very common part) and so a long provider id cannot blow
    the column width.
    """
    material = "\x1f".join(part or "" for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:48]

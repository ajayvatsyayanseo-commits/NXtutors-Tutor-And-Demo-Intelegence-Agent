"""Continuation tokens — pausing a MatchSession across another agent's detour.

The journey this exists for:

    parent: "I need a maths tutor for class 8, I don't have an account"
    -> we capture the requirement
    -> onboarding creates the account
    -> we resume and shortlist, **without asking for the subject again**

The token is what survives the detour. It is:

* **opaque and PII-free** — a session id and a signature, nothing else. It
  travels through another agent's process and may be logged there, so it must
  carry nothing that would be unsafe in that log;
* **signed** — a forged token must not be able to resume someone else's
  session or claim a different conversation;
* **short-lived** — a resume three days later should re-confirm the
  requirement rather than silently act on stale intent.

The requirement itself stays in *our* database keyed by the session id. The
token is a claim check, not a container.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

#: A paused match older than this is stale intent. The parent gets a short
#: confirmation instead of a silent resume.
DEFAULT_TTL_SECONDS = 6 * 3600
#: Token format version, not a secret. S105 matches on the identifier.
TOKEN_VERSION = "c1"  # noqa: S105


class ResumeFailure(StrEnum):
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    WRONG_CONVERSATION = "wrong_conversation"
    UNKNOWN_VERSION = "unknown_version"


class InvalidContinuation(Exception):
    def __init__(self, reason: ResumeFailure) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ContinuationClaim:
    """What a valid token proves."""

    session_id: str
    conversation_ref: str
    #: Why we paused, so the resume message can be specific.
    reason: str
    issued_at: int
    expires_at: int

    @property
    def age_seconds(self) -> int:
        return max(0, int(time.time()) - self.issued_at)


class ContinuationCodec:
    """Mints and verifies continuation tokens.

    HMAC rather than encryption: the contents are deliberately non-secret, so
    integrity is the only property that matters and a MAC is simpler to reason
    about than a cipher whose key rotation nobody has thought through.
    """

    def __init__(self, signing_key: str) -> None:
        if not signing_key:
            raise ValueError("continuation codec requires a signing key")
        self._key = signing_key.encode("utf-8")

    def issue(
        self,
        *,
        conversation_ref: str,
        reason: str,
        session_id: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: int | None = None,
    ) -> tuple[str, ContinuationClaim]:
        issued = int(now if now is not None else time.time())
        claim = ContinuationClaim(
            session_id=session_id or str(uuid.uuid4()),
            conversation_ref=conversation_ref,
            reason=reason[:64],
            issued_at=issued,
            expires_at=issued + ttl_seconds,
        )
        body = json.dumps(
            {
                "v": TOKEN_VERSION,
                "s": claim.session_id,
                "c": claim.conversation_ref,
                "r": claim.reason,
                "i": claim.issued_at,
                "e": claim.expires_at,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload = _b64(body)
        return f"{payload}.{_b64(self._mac(payload))}", claim

    def verify(
        self, token: str, *, conversation_ref: str, now: int | None = None
    ) -> ContinuationClaim:
        """Return the claim, or raise `InvalidContinuation`."""
        try:
            payload, signature = token.strip().split(".", 1)
        except ValueError as exc:
            raise InvalidContinuation(ResumeFailure.MALFORMED) from exc

        # Constant-time: a fast-fail comparison leaks the MAC byte by byte.
        if not hmac.compare_digest(_b64(self._mac(payload)), signature):
            raise InvalidContinuation(ResumeFailure.BAD_SIGNATURE)

        try:
            data = json.loads(_unb64(payload))
        except (ValueError, binascii.Error) as exc:
            raise InvalidContinuation(ResumeFailure.MALFORMED) from exc

        if data.get("v") != TOKEN_VERSION:
            raise InvalidContinuation(ResumeFailure.UNKNOWN_VERSION)

        claim = ContinuationClaim(
            session_id=str(data.get("s", "")),
            conversation_ref=str(data.get("c", "")),
            reason=str(data.get("r", "")),
            issued_at=int(data.get("i", 0)),
            expires_at=int(data.get("e", 0)),
        )
        if not claim.session_id or not claim.conversation_ref:
            raise InvalidContinuation(ResumeFailure.MALFORMED)

        # Binding to the conversation stops a token leaked from one parent's
        # detour being replayed into another parent's conversation.
        if not hmac.compare_digest(claim.conversation_ref, conversation_ref):
            raise InvalidContinuation(ResumeFailure.WRONG_CONVERSATION)

        if claim.expires_at <= int(now if now is not None else time.time()):
            raise InvalidContinuation(ResumeFailure.EXPIRED)

        return claim

    def try_verify(
        self, token: str | None, *, conversation_ref: str, now: int | None = None
    ) -> ContinuationClaim | None:
        """Verify without raising. An unusable token means "start fresh"."""
        if not token:
            return None
        try:
            return self.verify(token, conversation_ref=conversation_ref, now=now)
        except InvalidContinuation:
            return None

    def _mac(self, payload: str) -> bytes:
        return hmac.new(self._key, payload.encode("ascii"), sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def resume_message(reason: str, *, known_summary: str | None) -> str:
    """What we say when picking the conversation back up.

    Names what we already know, so the parent can see nothing was lost — the
    entire point of the token. Never mentions the other agent by name.
    """
    if known_summary:
        return f"All set. Picking up where we left off — {known_summary}."
    _ = reason
    return "All set. Let me carry on with the tutor shortlist."

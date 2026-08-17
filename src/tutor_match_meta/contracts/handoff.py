"""The Lead Intake → TutorMatch handoff contract.

**Pinned to the shape Lead Intake already sends**, read from
`nxtutors-lead-intake-agent/app/services/onboarding_router.py`:

    POST <AGENT>_WEBHOOK_URL
    X-NXTUTORS-INTERNAL-SECRET: <shared secret>
    {source, wa_message_id, wa_phone, message_text, timestamp,
     message_type, raw_payload}

    -> 200/202 {"status": "...", "reply_text": "..."}

That response shape is the whole ownership decision: **Lead Intake sends the
WhatsApp message, not us.** We return text; they deliver it. One webhook, one
sender, no double sends. TutorMatch's own outbound sender exists only for
environments where TutorMatch is deliberately given delivery ownership, and the
two are mutually exclusive (`OutboundOwnership`).

Implementing the same shape as onboarding means Lead Intake needs no code change
to call us — only `TUTOR_MATCHING_AGENT_WEBHOOK_URL` and a shared secret.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import SCHEMA_VERSION

#: The header Lead Intake already sends to the onboarding agent.
# The header *name*, not a credential. S105 matches on the identifier.
INTERNAL_SECRET_HEADER = "X-NXTUTORS-INTERNAL-SECRET"  # noqa: S105
#: Lead Intake gives the onboarding agent a 2s timeout, so a handoff consumer has
#: to answer fast. We enqueue and reply, never match inline.
HANDOFF_BUDGET_SECONDS = 2.0
MAX_MESSAGE_CHARS = 4_000


class HandoffStatus(StrEnum):
    """What TutorMatch did with the message. Lead Intake branches on this."""

    #: We handled it and `reply_text` should be sent to the parent.
    HANDLED = "handled"
    #: Accepted for async processing; the reply arrives via the outbound path.
    ACCEPTED = "accepted"
    #: Not ours. Lead Intake should keep ownership of this conversation.
    DECLINED = "declined"
    #: Ours, but we need another agent first (see `handoff_to`).
    NEEDS_HANDOFF = "needs_handoff"
    #: A human should take over.
    HUMAN_REVIEW = "human_review"
    #: We failed. Lead Intake should fall back to its own reply.
    ERROR = "error"


class OutboundOwnership(StrEnum):
    """Who delivers the WhatsApp message for this deployment.

    Exactly one is active at a time. Both being active is the double-send bug
    this enum exists to make impossible; `settings` validation enforces it.
    """

    #: Pattern A (default). We return `reply_text`; the caller sends it.
    CALLER_SENDS = "caller_sends"
    #: Pattern B. We emit `OutboundMessageRequestedV1` to a delivery service.
    TUTOR_MATCH_SENDS = "tutor_match_sends"


class LeadIntakeHandoffV1(BaseModel):
    """Exactly what Lead Intake posts. Extra fields are tolerated by design.

    `extra="allow"` is deliberate: Lead Intake may add fields at any time, and a
    strict model would turn an additive upstream change into an outage.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, frozen=True)

    source: str = Field(default="lead_intake_agent", max_length=64)
    wa_message_id: str = Field(max_length=128)
    #: Raw E.164 phone. Hashed immediately on receipt and never stored raw.
    wa_phone: str = Field(max_length=32)
    message_text: str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    timestamp: str | int | None = None
    message_type: str = Field(default="text", max_length=32)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    # --- optional enrichment Lead Intake may add later; all defaulted.
    lead_id: str | None = Field(default=None, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=64)
    intent: str | None = Field(default=None, max_length=64)
    #: Set when Lead Intake is resuming us after an onboarding detour.
    continuation_token: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _require_identity(self) -> Self:
        if not self.wa_message_id.strip():
            raise ValueError("wa_message_id is required for idempotency")
        if not self.wa_phone.strip():
            raise ValueError("wa_phone is required to identify the conversation")
        return self

    def effective_conversation_id(self) -> str:
        """Stable conversation lane. The caller hashes this before logging.

        Precedence is deliberately **phone before lead id**. A `lead_id` appears
        partway through a conversation (once intake resolves or creates the
        lead), so keying on it would silently rename the lane mid-conversation —
        splitting one parent across two states, two idempotency namespaces and
        two continuation scopes, which breaks resume in exactly the case it
        matters. The phone number is present on every single turn.

        An explicit `conversation_id` from Lead Intake still wins, because then
        both sides have agreed on the lane up front.
        """
        return (
            (self.conversation_id or "").strip()
            or f"wa:{self.wa_phone.strip()}"
            or (f"lead:{self.lead_id}" if self.lead_id else "")
        )


class HandoffResponseV1(BaseModel):
    """What we return. `status` + `reply_text` match what Lead Intake reads."""

    model_config = ConfigDict(frozen=True)

    status: HandoffStatus
    #: Sent verbatim by Lead Intake. None means "we have nothing to say".
    reply_text: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    schema_version: str = SCHEMA_VERSION
    trace_id: str | None = None
    match_session_id: str | None = None
    #: Set with NEEDS_HANDOFF: which agent should take the next turn.
    handoff_to: str | None = Field(default=None, max_length=64)
    #: Opaque, PII-free. Lead Intake returns it to us after the detour.
    continuation_token: str | None = Field(default=None, max_length=512)
    #: True when this exact `wa_message_id` was already processed.
    duplicate: bool = False
    #: Dependencies that were degraded during this turn. Diagnostics only.
    degraded: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.status is HandoffStatus.HANDLED and not self.reply_text:
            raise ValueError("HANDLED requires reply_text; use ACCEPTED for async")
        if self.status is HandoffStatus.NEEDS_HANDOFF and not self.handoff_to:
            raise ValueError("NEEDS_HANDOFF requires handoff_to")
        return self

    def to_body(self) -> dict[str, Any]:
        """Wire body. `status` and `reply_text` first — that is all the caller
        strictly needs; everything else is additive diagnostics."""
        body: dict[str, Any] = {"status": self.status.value, "reply_text": self.reply_text}
        if self.trace_id:
            body["trace_id"] = self.trace_id
        if self.match_session_id:
            body["match_session_id"] = self.match_session_id
        if self.handoff_to:
            body["handoff_to"] = self.handoff_to
        if self.continuation_token:
            body["continuation_token"] = self.continuation_token
        if self.duplicate:
            body["duplicate"] = True
        if self.degraded:
            body["degraded"] = list(self.degraded)
        body["schema_version"] = self.schema_version
        return body

    @classmethod
    def declined(cls, reason: str, *, trace_id: str | None = None) -> HandoffResponseV1:
        """Not our conversation. Lead Intake keeps it — we say nothing."""
        return cls(
            status=HandoffStatus.DECLINED,
            reply_text=None,
            trace_id=trace_id,
            degraded=(reason,) if reason else (),
        )

    @classmethod
    def errored(cls, reason: str, *, trace_id: str | None = None) -> HandoffResponseV1:
        """We failed. No reply_text: Lead Intake must use its own fallback
        rather than sending a half-formed message on our behalf."""
        return cls(
            status=HandoffStatus.ERROR, reply_text=None, trace_id=trace_id, degraded=(reason,)
        )


def parse_timestamp(value: str | int | None) -> datetime | None:
    """Lead Intake sends a WhatsApp epoch string, an int, or nothing."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=__import__("datetime").UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None

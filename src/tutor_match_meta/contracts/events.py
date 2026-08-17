"""Versioned `match.*.v1` events.

Two compatibility rules, both enforced by tests rather than convention:

1. **A released version never changes meaning.** Adding a field is fine;
   changing what an existing field means requires `.v2`.
2. **Consumers ignore unknown fields.** Every model here is `extra="allow"`, so
   a producer that adds a field cannot break a consumer that has not been
   redeployed. The alternative — strict models — turns every additive change
   into a coordinated multi-repo release.

Payloads carry pseudonymous references only. A consumer that needs the parent's
phone number resolves it from the website using the lead id, under its own
authorisation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType:
    """The published catalogue. Adding one is additive; renaming is not."""

    REQUESTED = "match.requested.v1"
    REQUIREMENTS_UPDATED = "match.requirements.updated.v1"
    READY = "match.ready.v1"
    SHORTLIST_GENERATED = "match.shortlist.generated.v1"
    SHORTLIST_SENT = "match.shortlist.sent.v1"
    CANDIDATE_SELECTED = "match.candidate.selected.v1"
    DEMO_REQUESTED = "match.demo.requested.v1"
    NO_CANDIDATE = "match.no_candidate.v1"
    HUMAN_HANDOFF_REQUESTED = "match.human_handoff.requested.v1"
    FEEDBACK_RECEIVED = "match.feedback.received.v1"
    REPLACEMENT_REQUESTED = "match.replacement.requested.v1"
    CLOSED = "match.closed.v1"
    #: Outbound delivery request, used only under `TUTOR_MATCH_SENDS`.
    OUTBOUND_MESSAGE_REQUESTED = "outbound.message.requested.v1"

    ALL: tuple[str, ...] = (
        REQUESTED,
        REQUIREMENTS_UPDATED,
        READY,
        SHORTLIST_GENERATED,
        SHORTLIST_SENT,
        CANDIDATE_SELECTED,
        DEMO_REQUESTED,
        NO_CANDIDATE,
        HUMAN_HANDOFF_REQUESTED,
        FEEDBACK_RECEIVED,
        REPLACEMENT_REQUESTED,
        CLOSED,
        OUTBOUND_MESSAGE_REQUESTED,
    )


class _Payload(BaseModel):
    """Base for every payload. Forward-compatible by construction."""

    model_config = ConfigDict(extra="allow", frozen=True)

    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: Peppered hash. Never a phone number or a raw conversation id.
    conversation_ref: str = Field(max_length=128)


class MatchRequestedV1(_Payload):
    match_session_id: str
    lead_id: str | None = None
    intent: str
    #: Present only when the parent has already stated it.
    subject: str | None = None
    student_class: str | None = None
    mode: str | None = None


class MatchRequirementsUpdatedV1(_Payload):
    match_session_id: str
    #: Field names only — the values stay in our database.
    known_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    ready_to_match: bool = False


class MatchShortlistGeneratedV1(_Payload):
    match_session_id: str
    #: Canonical website `user_id`s, in rank order.
    tutor_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    #: Detects a decision made under an unversioned policy edit.
    policy_checksum: str
    candidate_pool_size: int = 0
    weight_coverage: float = 0.0
    requires_human_review: bool = False


class MatchShortlistSentV1(_Payload):
    match_session_id: str
    tutor_ids: tuple[str, ...]
    #: Who actually delivered it. Exactly one agent may claim this.
    delivered_by: str


class MatchCandidateSelectedV1(_Payload):
    match_session_id: str
    tutor_id: str
    rank: int | None = None
    demo_requested: bool = False


class MatchDemoRequestedV1(_Payload):
    match_session_id: str
    tutor_id: str
    lead_id: str | None = None
    preferred_time: str | None = None
    mode: str | None = None


class MatchNoCandidateV1(_Payload):
    match_session_id: str
    #: The dominant hard-filter rule, so a coordinator knows what to relax.
    reason: str
    candidate_pool_size: int = 0
    top_rejection_rule: str | None = None


class MatchHumanHandoffRequestedV1(_Payload):
    match_session_id: str | None = None
    reason: str
    priority: str = "normal"
    #: Compact summary — staff should not have to read the raw transcript.
    summary: str = Field(default="", max_length=1_000)


class MatchFeedbackReceivedV1(_Payload):
    match_session_id: str
    tutor_id: str
    outcome: str
    detail: str | None = None


class MatchReplacementRequestedV1(_Payload):
    match_session_id: str | None = None
    #: Excluded from the new shortlist so we never re-suggest a bad fit.
    previous_tutor_id: str | None = None
    reason: str | None = None


class MatchClosedV1(_Payload):
    match_session_id: str
    outcome: str
    turns: int = 0


class OutboundMessageRequestedV1(_Payload):
    """Only emitted under `OutboundOwnership.TUTOR_MATCH_SENDS`.

    Under the default (`CALLER_SENDS`) this event is never produced — that
    mutual exclusion is what prevents a double send.
    """

    match_session_id: str | None = None
    #: Recipient is resolved by the delivery service from `conversation_ref`;
    #: the phone number is deliberately absent from the event.
    body: str = Field(max_length=4_096)
    #: conversation + source event + purpose. A retry reuses it exactly.
    idempotency_key: str = Field(max_length=128)
    purpose: str = Field(max_length=48)


PAYLOAD_TYPES: dict[str, type[_Payload]] = {
    EventType.REQUESTED: MatchRequestedV1,
    EventType.REQUIREMENTS_UPDATED: MatchRequirementsUpdatedV1,
    EventType.SHORTLIST_GENERATED: MatchShortlistGeneratedV1,
    EventType.SHORTLIST_SENT: MatchShortlistSentV1,
    EventType.CANDIDATE_SELECTED: MatchCandidateSelectedV1,
    EventType.DEMO_REQUESTED: MatchDemoRequestedV1,
    EventType.NO_CANDIDATE: MatchNoCandidateV1,
    EventType.HUMAN_HANDOFF_REQUESTED: MatchHumanHandoffRequestedV1,
    EventType.FEEDBACK_RECEIVED: MatchFeedbackReceivedV1,
    EventType.REPLACEMENT_REQUESTED: MatchReplacementRequestedV1,
    EventType.CLOSED: MatchClosedV1,
    EventType.OUTBOUND_MESSAGE_REQUESTED: OutboundMessageRequestedV1,
}


def parse_payload(event_type: str, data: dict[str, Any]) -> _Payload:
    """Parse a payload by event type. Unknown types raise, unknown fields do not.

    A consumer receiving an event type it has never heard of should skip it, not
    crash — but silently accepting an event whose *type* we do not model would
    hide a real contract break.
    """
    model = PAYLOAD_TYPES.get(event_type)
    if model is None:
        raise ValueError(f"unknown event type: {event_type}")
    return model.model_validate(data)


def outbound_idempotency_key(*, conversation_ref: str, source_event_id: str, purpose: str) -> str:
    """Conversation + source event + purpose, exactly as the brief requires.

    Including the purpose means a shortlist and a follow-up question caused by
    the same inbound event are distinct sends, while a retry of either is not.
    """
    from hashlib import sha256

    material = f"{conversation_ref}\x1f{source_event_id}\x1f{purpose}"
    return sha256(material.encode("utf-8")).hexdigest()[:48]

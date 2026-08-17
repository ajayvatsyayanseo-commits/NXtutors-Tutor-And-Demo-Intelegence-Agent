"""Inbound event contracts.

Two producers are supported and both are internal NXTutors agents:

1. the Lead Intake Agent's `lead.captured` / `lead.updated` events, whose shape is
   pinned to `nxtutors-lead-intake-agent/app/services/integrations/events.py`;
2. a WhatsApp turn handed off by whichever agent owns the Meta webhook.

This service does not own the Meta Cloud API webhook. Duplicating that signature
verification would create a second source of truth for the same payload.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import SCHEMA_VERSION
from tutor_match_meta.contracts.enrichment import EnrichmentV1

#: WhatsApp hard-caps a message body well below this; anything larger is abuse.
MAX_MESSAGE_CHARS = 4_000


class InboundKind(StrEnum):
    LEAD_EVENT = "lead_event"
    WHATSAPP_TURN = "whatsapp_turn"
    PARENT_SELECTION = "parent_selection"


class LeadEventV1(BaseModel):
    """Mirrors the lead-intake agent's `LeadCapturedEvent.to_payload()`.

    Note `class` is a Python keyword upstream and is serialised as `"class"`;
    the alias keeps the wire format identical rather than asking the producer to
    change. `phone_hash` is what upstream sends — never a raw phone number.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True, frozen=True)

    event_id: str = Field(max_length=128)
    event_type: Literal["lead.captured", "lead.updated"]
    lead_id: str | None = Field(default=None, max_length=128)
    phone_hash: str | None = Field(default=None, max_length=128)
    student_class: str | None = Field(default=None, alias="class", max_length=64)
    subject: str | None = Field(default=None, max_length=120)
    board: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=80)
    tuition_mode: str | None = Field(default=None, max_length=32)
    missing_fields: tuple[str, ...] = ()
    confidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    created_at: datetime | None = None

    @property
    def conversation_id(self) -> str:
        """Lead events carry no conversation of their own; the lead is the lane."""
        return f"lead:{self.lead_id or self.event_id}"


class WhatsAppTurnV1(BaseModel):
    """One inbound parent message, already de-Meta'd by the owning agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(max_length=128)
    conversation_id: str = Field(max_length=128)
    #: Provider-side message id. The dedup key for both SQS and the idempotency
    #: table — a Meta webhook retry must not produce a second shortlist.
    provider_message_id: str = Field(max_length=128)
    text: Annotated[str, Field(max_length=MAX_MESSAGE_CHARS)]
    phone_hash: str | None = Field(default=None, max_length=128)
    lead_id: str | None = Field(default=None, max_length=128)
    locale: str | None = Field(default=None, max_length=16)
    sent_at: datetime | None = None

    @model_validator(mode="after")
    def _reject_blank(self) -> Self:
        if not self.text.strip():
            raise ValueError("empty message text")
        return self


class ParentSelectionV1(BaseModel):
    """The parent picked a tutor from a shortlist we previously sent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(max_length=128)
    conversation_id: str = Field(max_length=128)
    match_session_id: str = Field(max_length=128)
    #: The public base64 ref from the shortlist, never a raw `user_id`.
    selected_public_ref: str = Field(max_length=256)
    demo_requested: bool = False
    selected_at: datetime | None = None


class InboundEnvelope(BaseModel):
    """What ingress validates, enqueues, and the worker consumes.

    Carrying the envelope (rather than the bare payload) through SQS means the
    worker inherits the trace id and the dedup key that ingress already
    established, instead of minting new ones and breaking the trace.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    kind: InboundKind
    trace_id: str = Field(max_length=64)
    #: SQS FIFO MessageGroupId. Same conversation => strict ordering, no races.
    conversation_id: str = Field(max_length=128)
    #: SQS FIFO MessageDeduplicationId and the idempotency-table key.
    dedup_key: str = Field(max_length=128)
    received_at: datetime
    source_agent: str = Field(max_length=64)
    #: E.164 address for the reply, populated **only** when this deployment owns
    #: delivery (`outbound_ownership=tutor_match_sends`). Under the default
    #: `caller_sends` it is None and nothing here ever holds a raw phone number.
    #: Carrying it on the envelope rather than re-deriving it downstream is what
    #: lets the outbound worker address a message at all — the inbound payload
    #: models only ever carry `phone_hash`.
    reply_to: str | None = Field(default=None, max_length=32)
    payload: LeadEventV1 | WhatsAppTurnV1 | ParentSelectionV1
    #: Attached by the internet-side enrich worker before this envelope is
    #: forwarded to the match queue. `None` means "nothing was pre-resolved" —
    #: the local stack and the test suite run that way, and the match worker
    #: falls back to resolving inline. In the deployed topology it is always
    #: populated, because the match worker has no route to the internet at all.
    #: See contracts/enrichment.py for why the boundary exists.
    enrichment: EnrichmentV1 | None = None

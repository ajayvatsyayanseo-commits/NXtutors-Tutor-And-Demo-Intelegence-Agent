"""Outbound messages — what the single send boundary accepts.

An `OutboundMessage` is a *request* to send, not a send. It carries everything
the boundary needs to decide whether sending is allowed: who it is for, which
conversation, whether it is a template or free-form, and the idempotency key
that makes a redelivered work item a no-op instead of a second message.

Free-form vs template is not a stylistic choice. Outside Meta's session window
only an approved template may be delivered, so a free-form message built for a
T-24h reminder is not "slightly wrong" — it is silently undeliverable.
`requires_template` makes that a checked property at construction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import SCHEMA_VERSION, Language, Party
from demo_command_center.shared.clock import ensure_utc

#: WhatsApp's own cap. A longer body is truncated by Meta mid-sentence.
MAX_BODY_CHARS = 4_096
#: Interactive replies cap at 3 buttons with 20 characters each.
MAX_BUTTONS = 3
MAX_BUTTON_CHARS = 20


class MessageKind(StrEnum):
    """What the message is for. Drives throttling and suppression policy."""

    QUESTION = "question"
    TUTOR_OPTIONS = "tutor_options"
    SLOT_PROPOSAL = "slot_proposal"
    CONFIRMATION = "confirmation"
    REMINDER = "reminder"
    TUTOR_REQUEST = "tutor_request"
    FOLLOWUP = "followup"
    OFFER = "offer"
    PAYMENT_LINK = "payment_link"
    WELCOME = "welcome"
    CANCELLATION = "cancellation"
    HUMAN_HANDOFF_NOTICE = "human_handoff_notice"


#: Kinds a parent's opt-out does not suppress. Deliberately tiny: a transactional
#: confirmation for something they actively bought is not marketing, but a
#: follow-up nudge is, and treating the two the same is how a service gets
#: reported. Cancellations are included because silently dropping one leaves
#: someone waiting for a demo that will not happen.
OPT_OUT_EXEMPT: frozenset[MessageKind] = frozenset(
    {
        MessageKind.CONFIRMATION,
        MessageKind.CANCELLATION,
        MessageKind.PAYMENT_LINK,
    }
)

#: Kinds that are almost always sent outside the 24h session window and
#: therefore must carry an approved template.
TEMPLATE_REQUIRED: frozenset[MessageKind] = frozenset(
    {
        MessageKind.REMINDER,
        MessageKind.TUTOR_REQUEST,
    }
)

#: Kinds that may be sent *from* a terminal state.
#:
#: These are the messages that announce the terminal state itself. The welcome
#: is sent on reaching CONVERTED and the cancellation notice on reaching
#: CANCELLED, so a blanket "terminal conversations send nothing" rule silently
#: swallowed both — a customer would be charged and never welcomed, or have
#: their demo cancelled and never told.
TERMINAL_ALLOWED: frozenset[MessageKind] = frozenset(
    {
        MessageKind.WELCOME,
        MessageKind.CANCELLATION,
    }
)


class Button(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Returned verbatim by Meta when tapped. The orchestrator dispatches on it,
    #: so it is an internal token, never a translated label.
    reply_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_:.-]+$")
    title: str = Field(min_length=1, max_length=MAX_BUTTON_CHARS)


class TemplateBinding(BaseModel):
    """An approved template plus its positional variables, already resolved."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=64)
    language: str = Field(min_length=2, max_length=8)
    #: Positional. Meta matches `{{1}}`..`{{n}}` by index, so order is the
    #: contract and a swapped pair silently sends the tutor's name as the date.
    variables: tuple[str, ...] = ()


class OutboundMessage(BaseModel):
    """A request to send one message. Only `orchestration/outbound.py` fulfils it."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    conversation_ref: str = Field(max_length=128)
    #: Opaque recipient handle. Resolved to a phone number inside the sender,
    #: at the last possible moment, and never stored on this object.
    recipient_ref: str = Field(max_length=128)
    audience: Party
    kind: MessageKind
    language: Language = Language.EN

    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    buttons: tuple[Button, ...] = ()
    template: TemplateBinding | None = None

    #: Deterministic. The outbound unique index is on this, so the same business
    #: action retried produces the same key and sends exactly once.
    idempotency_key: str = Field(max_length=128)
    demo_id: str | None = Field(default=None, max_length=64)
    trace_id: str = Field(default="", max_length=64)
    #: A message that has waited longer than this is dropped, not sent late. A
    #: T-15m reminder delivered after the demo is worse than no reminder.
    expires_at: datetime | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _deliverable(self) -> Self:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))
        if not self.body.strip() and self.template is None:
            raise ValueError("a message must have a body or a template")
        if len(self.buttons) > MAX_BUTTONS:
            raise ValueError(f"{len(self.buttons)} buttons exceeds WhatsApp's {MAX_BUTTONS}")
        ids = [b.reply_id for b in self.buttons]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate button reply ids: {ids}")
        if self.kind in TEMPLATE_REQUIRED and self.template is None:
            raise ValueError(
                f"{self.kind.value} is sent outside the session window and requires "
                "an approved template"
            )
        return self

    @property
    def requires_template(self) -> bool:
        return self.kind in TEMPLATE_REQUIRED

    @property
    def opt_out_exempt(self) -> bool:
        return self.kind in OPT_OUT_EXEMPT

    def expired(self, *, now: datetime) -> bool:
        return self.expires_at is not None and ensure_utc(now) >= self.expires_at


class SendOutcome(StrEnum):
    SENT = "sent"
    #: Already sent — the idempotency key matched a previous delivery.
    DUPLICATE = "duplicate"
    SUPPRESSED_NOT_OWNER = "suppressed_not_owner"
    SUPPRESSED_STATE = "suppressed_state"
    SUPPRESSED_OPT_OUT = "suppressed_opt_out"
    SUPPRESSED_QUIET_HOURS = "suppressed_quiet_hours"
    SUPPRESSED_RATE_LIMIT = "suppressed_rate_limit"
    SUPPRESSED_EXPIRED = "suppressed_expired"
    SUPPRESSED_NO_TEMPLATE = "suppressed_no_template"
    SUPPRESSED_GUARDRAIL = "suppressed_guardrail"
    FAILED = "failed"


class SendResult(BaseModel):
    """What the boundary did. Every suppression is a first-class outcome.

    Suppression is not an error and is not success. Collapsing it into either
    loses the operational question that actually matters — "how many reminders
    did we decide not to send, and why".
    """

    model_config = ConfigDict(frozen=True)

    outcome: SendOutcome
    idempotency_key: str = Field(max_length=128)
    provider_message_id: str = Field(default="", max_length=128)
    detail: str = Field(default="", max_length=200)
    sent_at: datetime | None = None

    @property
    def delivered(self) -> bool:
        return self.outcome is SendOutcome.SENT

    @property
    def suppressed(self) -> bool:
        return self.outcome.value.startswith("suppressed_")


def session_window_open(
    last_inbound_at: datetime | None, *, now: datetime, window_hours: int
) -> bool:
    """Whether Meta's free-form window is open for this conversation.

    `None` means we have never received an inbound message, which is a closed
    window — not an open one. Getting that default backwards means every
    first-contact message is attempted as free-form and silently rejected.
    """
    if last_inbound_at is None:
        return False
    return ensure_utc(now) - ensure_utc(last_inbound_at) < timedelta(hours=window_hours)

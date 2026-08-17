"""The domain event catalogue.

A closed enum, not free strings. Two reasons that both cost real money when
ignored: an EventBridge rule matching `demo.scheduled` goes silent forever if
someone emits `demo.scheduled.v2`, and a consumer that filters on an event name
cannot tell "no events" from "renamed event".

Every event is emitted through the transactional outbox (`glue/outbox.py`), so
"the row was written" and "the event was published" cannot disagree.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from demo_command_center.contracts.common import SCHEMA_VERSION


class DomainEvent(StrEnum):
    # --- lifecycle
    DEMO_REQUESTED = "demo.requested"
    REQUIREMENTS_COMPLETED = "demo.requirements_completed"
    TUTOR_SELECTED = "demo.tutor_selected"
    SLOT_HELD = "demo.slot_held"
    SLOT_HOLD_EXPIRED = "demo.slot_hold_expired"
    SCHEDULED = "demo.scheduled"
    RESCHEDULED = "demo.rescheduled"
    CANCELLED = "demo.cancelled"
    # --- attendance and outcome
    STUDENT_NO_SHOW = "demo.student_no_show"
    TUTOR_NO_SHOW = "demo.tutor_no_show"
    COMPLETED = "demo.completed"
    OBJECTIONS_EXTRACTED = "demo.objections_extracted"
    FORECAST_UPDATED = "demo.forecast_updated"
    FOLLOWUP_READY = "demo.followup_ready"
    # --- commercial
    DISCOUNT_OFFERED = "demo.discount_offered"
    DISCOUNT_ESCALATED = "demo.discount_escalated"
    PAYMENT_REQUESTED = "demo.payment_requested"
    PAYMENT_CONFIRMED = "payment.confirmed"
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    ONBOARDING_REQUESTED = "onboarding.requested"
    # --- conversation ownership
    HANDOFF_REQUESTED = "conversation.handoff_requested"
    HANDOFF_ACCEPTED = "conversation.handoff_accepted"
    HANDOFF_FAILED = "conversation.handoff_failed"
    HUMAN_HANDOFF_RAISED = "conversation.human_handoff_raised"
    # --- operations
    UNDERPERFORMANCE_DETECTED = "ops.underperformance_detected"


#: Events another agent is allowed to subscribe to. Anything outside this set is
#: internal: publishing it externally would leak state other services would then
#: start depending on, which is how a private field becomes a public contract.
PUBLIC_EVENTS: frozenset[DomainEvent] = frozenset(
    {
        DomainEvent.SCHEDULED,
        DomainEvent.RESCHEDULED,
        DomainEvent.CANCELLED,
        DomainEvent.COMPLETED,
        DomainEvent.STUDENT_NO_SHOW,
        DomainEvent.TUTOR_NO_SHOW,
        DomainEvent.PAYMENT_CONFIRMED,
        DomainEvent.SUBSCRIPTION_ACTIVATED,
        DomainEvent.ONBOARDING_REQUESTED,
        DomainEvent.HANDOFF_REQUESTED,
        DomainEvent.HANDOFF_ACCEPTED,
        DomainEvent.HANDOFF_FAILED,
        DomainEvent.UNDERPERFORMANCE_DETECTED,
    }
)


class EventRecord(BaseModel):
    """One outbox row's business content, before it is wrapped in an envelope."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    event: DomainEvent
    occurred_at: datetime
    conversation_ref: str = Field(max_length=128)
    demo_id: str | None = Field(default=None, max_length=64)
    #: Deterministic. The outbox unique index is on this, so a retry of the same
    #: business action produces the same key and inserts nothing the second time.
    idempotency_key: str = Field(max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def public(self) -> bool:
        return self.event in PUBLIC_EVENTS

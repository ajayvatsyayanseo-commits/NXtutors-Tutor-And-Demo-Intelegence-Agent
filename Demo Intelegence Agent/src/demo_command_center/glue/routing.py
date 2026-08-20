"""Capability routing and the typed events that cross worker boundaries.

Each capability runs in its own Lambda. This module is the only thing that
decides which one gets an event, and the only place that knows queue names.

Two properties it exists to enforce:

* **No capability mutates another's tables.** Every cross-capability effect is a
  typed `CapabilityEvent` routed here. `WRITES` records what each capability may
  write; `assert_write_allowed()` refuses anything else, so an undocumented
  reach into another aggregate fails a test rather than review.
* **Priority is queue topology, not a comment.** Payment and scheduling traffic
  never share a concurrency pool with reminders and analytics, so a reminder
  storm cannot starve a booking.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from demo_command_center.contracts.events import DomainEvent


class Capability(StrEnum):
    """The eight bounded modules, plus the three infrastructure workers."""

    ORCHESTRATOR = "orchestrator"
    SCHEDULING = "scheduling"
    REMINDERS = "reminders"
    FORECASTING = "forecasting"
    OBJECTIONS = "objections"
    CONVERSION = "conversion"
    DISCOUNTS = "discounts"
    PAID_TRANSITION = "paid_transition"
    MONITORING = "monitoring"
    OUTBOUND = "outbound"
    PERSISTENCE = "persistence"


class Lane(IntEnum):
    """Traffic class. Lower number wins when capacity is scarce.

    The whole point of separate lanes: a burst of reminder traffic and a burst
    of analytics traffic must not be able to delay a payment webhook or a
    booking. They are different queues with different reserved concurrency.
    """

    PAYMENT = 1
    SCHEDULING = 2
    CUSTOMER_OUTBOUND = 3
    REMINDERS = 4
    ANALYTICS = 5


#: Which lane each capability's work belongs to.
LANES: dict[Capability, Lane] = {
    Capability.PAID_TRANSITION: Lane.PAYMENT,
    Capability.ORCHESTRATOR: Lane.SCHEDULING,
    Capability.SCHEDULING: Lane.SCHEDULING,
    Capability.PERSISTENCE: Lane.SCHEDULING,
    Capability.OUTBOUND: Lane.CUSTOMER_OUTBOUND,
    Capability.CONVERSION: Lane.CUSTOMER_OUTBOUND,
    Capability.REMINDERS: Lane.REMINDERS,
    Capability.DISCOUNTS: Lane.ANALYTICS,
    Capability.FORECASTING: Lane.ANALYTICS,
    Capability.OBJECTIONS: Lane.ANALYTICS,
    Capability.MONITORING: Lane.ANALYTICS,
}

#: Tables each capability may write. Anything else is a boundary violation.
#: Read access is not restricted here — reading another aggregate is
#: occasionally legitimate; writing it never is.
WRITES: dict[Capability, frozenset[str]] = {
    Capability.ORCHESTRATOR: frozenset(
        {
            "dcc_conversations",
            "dcc_conversation_state",
            "dcc_state_transitions",
            "dcc_idempotency_keys",
            "dcc_inbound_events",
            "dcc_outbox_events",
            "dcc_tool_executions",
            "dcc_audit_events",
            "dcc_demo_requests",
            "dcc_demo_requirements",
            "dcc_tutor_candidate_snapshots",
        }
    ),
    Capability.SCHEDULING: frozenset(
        {"dcc_slot_holds", "dcc_demos", "dcc_demo_attendees", "dcc_tutor_confirmation_requests"}
    ),
    Capability.REMINDERS: frozenset({"dcc_demo_reminders"}),
    Capability.FORECASTING: frozenset(
        {"dcc_conversion_forecasts", "dcc_forecast_versions", "dcc_demo_quality_scores"}
    ),
    Capability.OBJECTIONS: frozenset({"dcc_objection_analyses"}),
    Capability.CONVERSION: frozenset(set()),
    Capability.DISCOUNTS: frozenset({"dcc_discount_decisions"}),
    Capability.PAID_TRANSITION: frozenset(
        {
            "dcc_payment_orders",
            "dcc_payment_events",
            "dcc_subscription_activation_attempts",
            "dcc_handoffs",
        }
    ),
    Capability.MONITORING: frozenset(
        {"dcc_regional_metric_rollups", "dcc_underperformance_alerts", "dcc_human_handoff_cases"}
    ),
    Capability.OUTBOUND: frozenset({"dcc_message_log"}),
    Capability.PERSISTENCE: frozenset(set()),  # a transport, not an owner
}


class BoundaryViolation(Exception):
    def __init__(self, capability: Capability, table: str) -> None:
        super().__init__(f"{capability.value} may not write {table}")
        self.capability = capability
        self.table = table


def assert_write_allowed(capability: Capability, table: str) -> None:
    """The rule that stops one capability reaching into another's aggregate."""
    if table not in WRITES.get(capability, frozenset()):
        raise BoundaryViolation(capability, table)


def owner_of(table: str) -> Capability | None:
    for capability, tables in WRITES.items():
        if table in tables:
            return capability
    return None


#: Which capability handles each domain event. Events with no handler are
#: notifications for external consumers, not internal work.
ROUTES: dict[DomainEvent, Capability] = {
    DomainEvent.DEMO_REQUESTED: Capability.ORCHESTRATOR,
    DomainEvent.REQUIREMENTS_COMPLETED: Capability.ORCHESTRATOR,
    DomainEvent.TUTOR_SELECTED: Capability.SCHEDULING,
    DomainEvent.SLOT_HELD: Capability.SCHEDULING,
    DomainEvent.SLOT_HOLD_EXPIRED: Capability.SCHEDULING,
    DomainEvent.SCHEDULED: Capability.REMINDERS,
    DomainEvent.RESCHEDULED: Capability.REMINDERS,
    DomainEvent.CANCELLED: Capability.REMINDERS,
    DomainEvent.COMPLETED: Capability.OBJECTIONS,
    DomainEvent.OBJECTIONS_EXTRACTED: Capability.FORECASTING,
    DomainEvent.FORECAST_UPDATED: Capability.CONVERSION,
    DomainEvent.FOLLOWUP_READY: Capability.OUTBOUND,
    DomainEvent.DISCOUNT_OFFERED: Capability.CONVERSION,
    DomainEvent.DISCOUNT_ESCALATED: Capability.MONITORING,
    DomainEvent.PAYMENT_REQUESTED: Capability.PAID_TRANSITION,
    DomainEvent.PAYMENT_CONFIRMED: Capability.PAID_TRANSITION,
    DomainEvent.PAYMENT_FAILED: Capability.CONVERSION,
    DomainEvent.SUBSCRIPTION_ACTIVATED: Capability.PAID_TRANSITION,
    DomainEvent.ONBOARDING_REQUESTED: Capability.PAID_TRANSITION,
    DomainEvent.STUDENT_NO_SHOW: Capability.MONITORING,
    DomainEvent.TUTOR_NO_SHOW: Capability.MONITORING,
    DomainEvent.HUMAN_HANDOFF_RAISED: Capability.MONITORING,
    DomainEvent.UNDERPERFORMANCE_DETECTED: Capability.MONITORING,
}


@dataclass(frozen=True, slots=True)
class CapabilityEvent:
    """A typed event crossing a worker boundary.

    Carries the full correlation chain. `message_group_id` is the conversation
    reference wherever ordering matters, which is what makes FIFO per
    conversation work without serialising unrelated conversations.
    """

    event: DomainEvent
    capability: Capability
    conversation_ref: str
    idempotency_key: str
    trace_id: str = ""
    correlation_id: str = ""
    causation_id: str | None = None
    payload: dict[str, Any] | None = None
    attempt: int = 1

    @property
    def lane(self) -> Lane:
        return LANES[self.capability]

    @property
    def message_group_id(self) -> str:
        """FIFO ordering key.

        Analytics work is deliberately grouped by *event type* rather than by
        conversation: two forecasts for different conversations have no ordering
        relationship, and grouping them by conversation would needlessly
        serialise a queue that could run wide.
        """
        if self.lane is Lane.ANALYTICS:
            return f"analytics:{self.event.value}"
        return self.conversation_ref

    def as_message(self) -> dict[str, Any]:
        return {
            "event": self.event.value,
            "capability": self.capability.value,
            "conversation_ref": self.conversation_ref,
            "idempotency_key": self.idempotency_key,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "payload": self.payload or {},
            "attempt": self.attempt,
        }

    def retried(self) -> CapabilityEvent:
        from dataclasses import replace

        return replace(self, attempt=self.attempt + 1)


def route(event: DomainEvent) -> Capability | None:
    """Which worker handles this event. None means it is external-only."""
    return ROUTES.get(event)


def routing_invariants() -> list[str]:
    """Structural checks on the routing table. Must be empty."""
    problems: list[str] = []

    for capability in Capability:
        if capability not in LANES:
            problems.append(f"{capability.value} has no lane")
        if capability not in WRITES:
            problems.append(f"{capability.value} has no declared writes")

    # No table may be writable by two capabilities — that is exactly the shared
    # ownership this module exists to prevent.
    seen: dict[str, Capability] = {}
    for capability, tables in WRITES.items():
        for table in tables:
            if table in seen:
                problems.append(
                    f"{table} is writable by both {seen[table].value} and {capability.value}"
                )
            seen[table] = capability

    for event, capability in ROUTES.items():
        if capability not in LANES:
            problems.append(f"{event.value} routes to an unlaned capability")

    return problems

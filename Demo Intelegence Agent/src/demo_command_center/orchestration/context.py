"""Everything the orchestrator needs, in one place.

`Dependencies` is a plain dataclass of protocols. It is assembled once in
`bootstrap.py` and never constructed inside a handler, which is what lets the
E2E harness build the identical object graph with fakes and exercise the real
orchestrator rather than a test-only variant of it.

`TurnContext` is the per-turn scratchpad: the snapshot, the ownership record and
the facts guards will read. Facts are assembled here, from repositories, *before*
the state machine runs — never from user text, and never lazily inside a guard,
because a guard that does I/O makes the concurrency tests meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from demo_command_center.capabilities.conversion.service import ConversionCapability
from demo_command_center.capabilities.discounts.service import DiscountCapability
from demo_command_center.capabilities.forecasting.service import ForecastCapability
from demo_command_center.capabilities.monitoring.service import MonitoringCapability
from demo_command_center.capabilities.objection_extraction.service import (
    ObjectionExtractionCapability,
)
from demo_command_center.capabilities.paid_transition.service import PaidTransitionCapability
from demo_command_center.capabilities.reminders.service import ReminderCapability
from demo_command_center.capabilities.scheduling.service import SchedulingCapability
from demo_command_center.contracts.ownership import Ownership
from demo_command_center.contracts.ports import (
    AgentBusPort,
    NxtutorsGatewayPort,
    SchedulerPort,
    TutorIntelligencePort,
)
from demo_command_center.domain.pricing import DiscountStatus
from demo_command_center.orchestration.outbound import OutboundBoundary
from demo_command_center.repositories.ports import (
    AnalysisRepository,
    CommerceRepository,
    ConversationRepository,
    DemoRepository,
    IdempotencyRepository,
    OperationsRepository,
    OutboxRepository,
    ReminderRepository,
    SlotRepository,
)
from demo_command_center.security.pii import Pseudonymiser
from demo_command_center.shared.clock import Clock
from demo_command_center.state.machine import StateMachine, StateSnapshot
from demo_command_center.state.triggers import Trigger


@dataclass(slots=True)
class Dependencies:
    """The composition root's output. Protocols only — no concrete clients."""

    # --- infrastructure
    clock: Clock
    machine: StateMachine
    pseudonymiser: Pseudonymiser

    # --- persistence
    conversations: ConversationRepository
    idempotency: IdempotencyRepository
    demos: DemoRepository
    slots: SlotRepository
    reminders: ReminderRepository
    outbox: OutboxRepository
    analysis: AnalysisRepository
    commerce: CommerceRepository
    operations: OperationsRepository

    # --- the one send path
    outbound: OutboundBoundary

    # --- capabilities
    scheduling: SchedulingCapability
    reminder_policy: ReminderCapability
    forecasting: ForecastCapability
    objections: ObjectionExtractionCapability
    conversion: ConversionCapability
    discounts: DiscountCapability
    paid: PaidTransitionCapability
    monitoring: MonitoringCapability

    # --- external agents
    tutors: TutorIntelligencePort
    gateway: NxtutorsGatewayPort
    agents: AgentBusPort
    scheduler: SchedulerPort

    # --- behaviour flags, all from settings
    onboarding_webhook_url: str = ""
    handoff_ttl_seconds: int = 3_600
    idempotency_ttl_seconds: int = 86_400
    scheduling_enabled: bool = True
    reminders_enabled: bool = True
    payments_enabled: bool = True
    discounts_enabled: bool = True


@dataclass(slots=True)
class TurnContext:
    """Per-turn state. Mutable, short-lived, never shared across turns."""

    conversation_ref: str
    trace_id: str
    correlation_id: str
    now: datetime
    snapshot: StateSnapshot
    ownership: Ownership
    #: The trigger that caused this turn. Commands shared by more than one
    #: trigger need it: `TRY_FALLBACK_TUTOR` runs on both a decline and an
    #: expiry, and only the expiry should notify the tutor — they already know
    #: they declined.
    trigger: Trigger | None = None
    #: Guard inputs. Assembled from repositories before the machine runs.
    facts: dict[str, Any] = field(default_factory=dict)
    #: Messages produced this turn. Sent by the orchestrator, not by capabilities.
    outbox: list[Any] = field(default_factory=list)
    #: Dependencies that were degraded. Diagnostics, surfaced in the response.
    degraded: list[str] = field(default_factory=list)
    causation_id: str | None = None

    def fact(self, name: str, value: Any) -> None:
        self.facts[name] = value

    def merge(self, **facts: Any) -> None:
        self.facts.update(facts)


async def assemble_facts(deps: Dependencies, ctx: TurnContext) -> None:
    """Load every guard input for this conversation.

    One place, one time, before the machine runs. The alternative — each guard
    reaching for what it needs — makes the transition table's behaviour depend
    on I/O interleaving, and makes `tests/unit/test_state_machine.py` unable to
    construct a scenario without a database.

    Facts persisted by an earlier turn are the base layer; derived values are
    layered on top and **never overwrite a known value with `None`**. That
    matters for `tutor_ref`: it is established at selection time, before a demo
    row exists, so deriving it from the (absent) demo would erase it and the
    next turn would fail with "no tutor selected".
    """
    ctx.facts.update(ctx.snapshot.facts)

    demo = await deps.demos.for_conversation(ctx.conversation_ref)
    request = await deps.demos.request_for_conversation(ctx.conversation_ref)
    hold = await deps.slots.active_hold_for(ctx.conversation_ref, now=ctx.now)
    candidates = await deps.demos.load_candidates(ctx.conversation_ref)

    ctx.merge(
        has_demo=demo is not None,
        missing_requirements=request.requirement.missing() if request else ("service",),
        has_candidates=bool(candidates),
        candidate_count=len(candidates),
        hold_id=hold.hold_id if hold else None,
        hold_expired=bool(hold and hold.expired(now=ctx.now)),
    )
    _set_if_known(ctx, "demo_id", demo.demo_id if demo else None)
    _set_if_known(ctx, "calendar_event_id", demo.calendar_event_id if demo else None)
    _set_if_known(ctx, "tutor_ref", demo.tutor_ref if demo else None)

    # Recomputed every turn against the *current* snapshot, so a candidate list
    # that was replaced by a re-match invalidates a stale selection.
    selected = ctx.facts.get("tutor_ref")
    ctx.fact(
        "tutor_in_snapshot",
        bool(selected and any(c.tutor_ref == selected for c in candidates)),
    )

    # "There is an authorised price to charge" — not "a discount was granted".
    # A decision awaiting human approval is the only one that blocks payment;
    # a denied or inapplicable discount still leaves the list price payable.
    decision = await deps.commerce.load_decision(demo.demo_id) if demo else None
    ctx.fact(
        "approved_quote",
        bool(decision and decision.status is not DiscountStatus.ESCALATED),
    )

    order = await deps.commerce.order_for_conversation(ctx.conversation_ref)
    if order is not None:
        activation = await deps.commerce.activation_for(order.order_ref)
        ctx.merge(order_ref=order.order_ref)
        _set_if_known(ctx, "subscription_ref", activation.subscription_ref if activation else None)


def _set_if_known(ctx: TurnContext, name: str, value: Any) -> None:
    """Write only a real value. A `None` leaves any persisted fact intact."""
    if value is not None:
        ctx.fact(name, value)

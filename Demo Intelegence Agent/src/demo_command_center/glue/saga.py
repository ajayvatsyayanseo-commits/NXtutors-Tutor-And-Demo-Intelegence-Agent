"""Sagas: multi-step operations with an external side effect in the middle.

Two of them matter, and both fail in ways that are expensive if handled naively.

**Booking** — hold, persist intent, create the calendar event, persist the
result, confirm. The failure that must never happen is telling a parent the
demo is confirmed when Google refused. So `BOOKING` marks the confirm step as
the *only* customer-visible one, and it runs last, after the event id exists.

**Payment** — verify, persist the event, activate, hand off. Every step after
verification is idempotent on a key derived from the order, so a timeout
anywhere is safe to replay. Activation succeeding and the response being lost
is the specific case this is built for: the retry presents the same key and the
gateway returns the existing subscription.

A saga here is *declarative*. It records what has happened so a resumed
execution knows where it is, and what to compensate if it cannot continue. The
steps themselves live in the capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SagaName(StrEnum):
    BOOKING = "booking"
    PAYMENT = "payment"


class StepState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    COMPENSATED = "compensated"


@dataclass(frozen=True, slots=True)
class Step:
    """One step, its compensation, and how visible it is."""

    name: str
    #: What to run if a later step fails. Empty means nothing to undo.
    compensation: str = ""
    #: True when this step is visible to a customer. At most one per saga, and
    #: it must be last — a customer-visible step cannot be compensated by
    #: anything except an apology.
    customer_visible: bool = False
    #: True when re-running with the same key is a no-op.
    idempotent: bool = True
    max_attempts: int = 3


BOOKING_STEPS: tuple[Step, ...] = (
    Step(name="hold_slot", compensation="release_slot_hold"),
    Step(name="persist_intent", compensation="clear_intent"),
    Step(name="create_calendar_event", compensation="cancel_calendar_event", max_attempts=2),
    Step(name="persist_calendar_result"),
    # Last, and the only customer-visible step. Nothing after it can fail in a
    # way that makes the confirmation a lie.
    Step(name="send_confirmation", customer_visible=True),
)

PAYMENT_STEPS: tuple[Step, ...] = (
    # No compensation: a verified payment event is a fact about the world. It
    # is recorded, never undone.
    Step(name="verify_webhook", max_attempts=1),
    Step(name="persist_payment_event", max_attempts=1),
    Step(name="activate_subscription", max_attempts=3),
    Step(name="handoff_to_onboarding", max_attempts=3),
    Step(name="send_welcome", customer_visible=True),
)

SAGAS: dict[SagaName, tuple[Step, ...]] = {
    SagaName.BOOKING: BOOKING_STEPS,
    SagaName.PAYMENT: PAYMENT_STEPS,
}


@dataclass(slots=True)
class SagaRun:
    """The recorded progress of one saga instance. Persisted, then resumed."""

    saga: SagaName
    conversation_ref: str
    #: Deterministic per business action. Two workers resuming the same run
    #: compute the same key and one of them loses the idempotency claim.
    correlation_key: str
    started_at: datetime
    steps: dict[str, StepState] = field(default_factory=dict)
    failure: str = ""

    def __post_init__(self) -> None:
        for step in SAGAS[self.saga]:
            self.steps.setdefault(step.name, StepState.PENDING)

    @property
    def definition(self) -> tuple[Step, ...]:
        return SAGAS[self.saga]

    def next_step(self) -> Step | None:
        """The first step not yet done. None means the saga finished."""
        for step in self.definition:
            if self.steps[step.name] is not StepState.DONE:
                return step
        return None

    def mark(self, step_name: str, state: StepState) -> None:
        if step_name not in self.steps:
            raise KeyError(f"{self.saga.value} has no step {step_name!r}")
        self.steps[step_name] = state

    @property
    def complete(self) -> bool:
        return all(state is StepState.DONE for state in self.steps.values())

    @property
    def failed(self) -> bool:
        return any(state is StepState.FAILED for state in self.steps.values())

    def compensations(self) -> tuple[str, ...]:
        """What to undo, in reverse order of completion.

        Reverse order matters: releasing the slot hold before cancelling the
        calendar event would free a slot that still has an event on it.
        """
        out: list[str] = []
        for step in reversed(self.definition):
            if self.steps[step.name] is StepState.DONE and step.compensation:
                out.append(step.compensation)
        return tuple(out)

    def customer_was_told(self) -> bool:
        """Whether anything customer-visible already happened.

        The question that decides whether a failure can be silently retried or
        needs an apology. A saga that has already sent a confirmation cannot be
        rolled back quietly.
        """
        return any(
            step.customer_visible and self.steps[step.name] is StepState.DONE
            for step in self.definition
        )

    def as_row(self) -> dict[str, object]:
        return {
            "saga": self.saga.value,
            "conversation_ref": self.conversation_ref,
            "correlation_key": self.correlation_key,
            "started_at": self.started_at.isoformat(),
            "steps": {name: state.value for name, state in self.steps.items()},
            "complete": self.complete,
            "failed": self.failed,
            "failure": self.failure,
        }


def saga_invariants() -> list[str]:
    """Structural checks on every saga definition. Must be empty."""
    problems: list[str] = []
    for name, steps in SAGAS.items():
        visible = [s for s in steps if s.customer_visible]
        if len(visible) > 1:
            problems.append(f"{name.value}: more than one customer-visible step")
        if visible and steps[-1] is not visible[0]:
            # If it is not last, a later failure makes an already-sent message
            # a lie, and there is no compensation for that.
            problems.append(f"{name.value}: the customer-visible step must be last")
        for step in steps:
            if step.customer_visible and step.compensation:
                problems.append(f"{name.value}.{step.name}: a sent message cannot be compensated")
            if step.max_attempts < 1:
                problems.append(f"{name.value}.{step.name}: max_attempts must be at least 1")
        names = [s.name for s in steps]
        if len(set(names)) != len(names):
            problems.append(f"{name.value}: duplicate step names")
    return problems

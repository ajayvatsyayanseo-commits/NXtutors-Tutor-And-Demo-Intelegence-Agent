"""Deterministic conversation state machine.

The transition table is data, and it is exhaustive. An LLM can *suggest* intent,
but a transition that is not in the table simply does not happen — `transition()`
raises `InvalidTransition`, the current state is preserved, and the event is
recorded. That is the difference between a state machine and a model deciding
what happens next.

Concurrency is handled with optimistic locking: every persisted state carries a
`lock_version`, and a write that does not match the version it read loses. Two
WhatsApp messages arriving milliseconds apart cannot both advance the state.
(SQS FIFO with `MessageGroupId = conversation_id` makes that rare; the lock makes
it impossible.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

FSM_VERSION = "1"


class ConversationState(StrEnum):
    NEW = "NEW"
    COLLECTING_REQUIREMENTS = "COLLECTING_REQUIREMENTS"
    READY_TO_MATCH = "READY_TO_MATCH"
    MATCHING = "MATCHING"
    SHORTLIST_READY = "SHORTLIST_READY"
    AWAITING_SELECTION = "AWAITING_SELECTION"
    DEMO_REQUESTED = "DEMO_REQUESTED"
    DEMO_HANDOFF = "DEMO_HANDOFF"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    NO_MATCH = "NO_MATCH"
    CLOSED = "CLOSED"
    ERROR_RECOVERABLE = "ERROR_RECOVERABLE"


class Trigger(StrEnum):
    MESSAGE_RECEIVED = "message_received"
    REQUIREMENTS_INCOMPLETE = "requirements_incomplete"
    REQUIREMENTS_COMPLETE = "requirements_complete"
    MATCH_STARTED = "match_started"
    SHORTLIST_GENERATED = "shortlist_generated"
    NO_CANDIDATES = "no_candidates"
    SHORTLIST_DELIVERED = "shortlist_delivered"
    PARENT_SELECTED = "parent_selected"
    DEMO_REQUESTED = "demo_requested"
    DEMO_HANDED_OFF = "demo_handed_off"
    REQUIREMENTS_CHANGED = "requirements_changed"
    HUMAN_HANDOFF = "human_handoff"
    RECOVERABLE_ERROR = "recoverable_error"
    RETRY = "retry"
    CLOSE = "close"


#: States from which nothing further happens. A message arriving here starts a
#: fresh conversation rather than mutating a finished one.
TERMINAL: frozenset[ConversationState] = frozenset(
    {ConversationState.CLOSED, ConversationState.DEMO_HANDOFF}
)

#: The complete transition table. `(from_state, trigger) -> to_state`.
#: Anything absent is forbidden — that is the point.
TRANSITIONS: dict[tuple[ConversationState, Trigger], ConversationState] = {
    # --- intake
    (ConversationState.NEW, Trigger.MESSAGE_RECEIVED): ConversationState.COLLECTING_REQUIREMENTS,
    (ConversationState.NEW, Trigger.REQUIREMENTS_COMPLETE): ConversationState.READY_TO_MATCH,
    (ConversationState.NEW, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    (ConversationState.COLLECTING_REQUIREMENTS, Trigger.MESSAGE_RECEIVED): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.COLLECTING_REQUIREMENTS, Trigger.REQUIREMENTS_INCOMPLETE): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.COLLECTING_REQUIREMENTS, Trigger.REQUIREMENTS_COMPLETE): (
        ConversationState.READY_TO_MATCH
    ),
    (ConversationState.COLLECTING_REQUIREMENTS, Trigger.HUMAN_HANDOFF): (
        ConversationState.HUMAN_REVIEW
    ),
    (ConversationState.COLLECTING_REQUIREMENTS, Trigger.CLOSE): ConversationState.CLOSED,
    # --- matching
    (ConversationState.READY_TO_MATCH, Trigger.MATCH_STARTED): ConversationState.MATCHING,
    (ConversationState.READY_TO_MATCH, Trigger.MESSAGE_RECEIVED): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.READY_TO_MATCH, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    (ConversationState.MATCHING, Trigger.SHORTLIST_GENERATED): ConversationState.SHORTLIST_READY,
    (ConversationState.MATCHING, Trigger.NO_CANDIDATES): ConversationState.NO_MATCH,
    (ConversationState.MATCHING, Trigger.RECOVERABLE_ERROR): ConversationState.ERROR_RECOVERABLE,
    (ConversationState.MATCHING, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    # --- delivery and selection
    (ConversationState.SHORTLIST_READY, Trigger.SHORTLIST_DELIVERED): (
        ConversationState.AWAITING_SELECTION
    ),
    (ConversationState.SHORTLIST_READY, Trigger.RECOVERABLE_ERROR): (
        ConversationState.ERROR_RECOVERABLE
    ),
    (ConversationState.AWAITING_SELECTION, Trigger.PARENT_SELECTED): (
        ConversationState.DEMO_REQUESTED
    ),
    (ConversationState.AWAITING_SELECTION, Trigger.DEMO_REQUESTED): (
        ConversationState.DEMO_REQUESTED
    ),
    # A parent who changes their mind mid-shortlist goes back to collection
    # rather than being told the conversation has moved on.
    (ConversationState.AWAITING_SELECTION, Trigger.REQUIREMENTS_CHANGED): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.AWAITING_SELECTION, Trigger.MESSAGE_RECEIVED): (
        ConversationState.AWAITING_SELECTION
    ),
    (ConversationState.AWAITING_SELECTION, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    (ConversationState.AWAITING_SELECTION, Trigger.CLOSE): ConversationState.CLOSED,
    # --- demo
    (ConversationState.DEMO_REQUESTED, Trigger.DEMO_HANDED_OFF): ConversationState.DEMO_HANDOFF,
    (ConversationState.DEMO_REQUESTED, Trigger.RECOVERABLE_ERROR): (
        ConversationState.ERROR_RECOVERABLE
    ),
    (ConversationState.DEMO_REQUESTED, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    # --- no match
    (ConversationState.NO_MATCH, Trigger.REQUIREMENTS_CHANGED): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.NO_MATCH, Trigger.MESSAGE_RECEIVED): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.NO_MATCH, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    (ConversationState.NO_MATCH, Trigger.CLOSE): ConversationState.CLOSED,
    # --- recovery
    (ConversationState.ERROR_RECOVERABLE, Trigger.RETRY): ConversationState.READY_TO_MATCH,
    (ConversationState.ERROR_RECOVERABLE, Trigger.MESSAGE_RECEIVED): (
        ConversationState.COLLECTING_REQUIREMENTS
    ),
    (ConversationState.ERROR_RECOVERABLE, Trigger.HUMAN_HANDOFF): ConversationState.HUMAN_REVIEW,
    # --- human review is a one-way door until a human closes it
    (ConversationState.HUMAN_REVIEW, Trigger.CLOSE): ConversationState.CLOSED,
}


class InvalidTransition(Exception):
    """A transition that is not in the table. Fails safely, state unchanged."""

    def __init__(self, state: ConversationState, trigger: Trigger) -> None:
        super().__init__(f"cannot apply {trigger} from {state}")
        self.state = state
        self.trigger = trigger


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    state_before: ConversationState
    state_after: ConversationState
    trigger: Trigger
    at: datetime
    trace_id: str | None = None


@dataclass(slots=True)
class ConversationSnapshot:
    """Persisted conversation state, with its optimistic lock."""

    conversation_id: str
    state: ConversationState = ConversationState.NEW
    fsm_version: str = FSM_VERSION
    lock_version: int = 0
    turn_count: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    history: tuple[TransitionRecord, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


def can_transition(state: ConversationState, trigger: Trigger) -> bool:
    return (state, trigger) in TRANSITIONS


def next_state(state: ConversationState, trigger: Trigger) -> ConversationState:
    try:
        return TRANSITIONS[(state, trigger)]
    except KeyError:
        raise InvalidTransition(state, trigger) from None


def transition(
    snapshot: ConversationSnapshot,
    trigger: Trigger,
    *,
    now: datetime | None = None,
    trace_id: str | None = None,
    history_limit: int = 40,
) -> ConversationSnapshot:
    """Apply a trigger, returning a NEW snapshot with the lock incremented.

    Never mutates the input: the caller still holds the version it read, which is
    what the repository compares against on write.
    """
    target = next_state(snapshot.state, trigger)
    stamp = now or datetime.now(UTC)
    record = TransitionRecord(
        state_before=snapshot.state,
        state_after=target,
        trigger=trigger,
        at=stamp,
        trace_id=trace_id,
    )
    return ConversationSnapshot(
        conversation_id=snapshot.conversation_id,
        state=target,
        fsm_version=FSM_VERSION,
        lock_version=snapshot.lock_version + 1,
        turn_count=snapshot.turn_count + (1 if trigger is Trigger.MESSAGE_RECEIVED else 0),
        updated_at=stamp,
        history=(*snapshot.history, record)[-history_limit:],
    )


def try_transition(
    snapshot: ConversationSnapshot,
    trigger: Trigger,
    *,
    now: datetime | None = None,
    trace_id: str | None = None,
) -> tuple[ConversationSnapshot, bool]:
    """Transition if legal, else return the snapshot unchanged.

    For the many places where an illegal trigger is a normal occurrence — a
    parent replying to a shortlist that was never delivered, a duplicate
    completion event — and not an error worth raising.
    """
    if not can_transition(snapshot.state, trigger):
        return snapshot, False
    return transition(snapshot, trigger, now=now, trace_id=trace_id), True


class OptimisticLockError(Exception):
    """Another writer advanced this conversation first. The caller re-reads."""

    def __init__(self, conversation_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"conversation {conversation_id}: expected lock_version {expected}, found {actual}"
        )
        self.conversation_id = conversation_id
        self.expected = expected
        self.actual = actual


def reachable_states() -> set[ConversationState]:
    """States reachable from NEW. Used by a test to catch orphaned states."""
    seen = {ConversationState.NEW}
    frontier = [ConversationState.NEW]
    while frontier:
        current = frontier.pop()
        for (source, _), target in TRANSITIONS.items():
            if source is current and target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen

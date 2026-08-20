"""The state machine. Pure: no I/O, no clock reads, no persistence.

`StateMachine.fire()` takes a snapshot and an event and returns either a
`TransitionResult` describing what *should* happen, or raises. It never writes
anything. Persistence, optimistic locking and command execution belong to the
orchestrator, which is what makes every rule in `transitions.py` testable
without a database.

The order of checks is deliberate and is itself a security property:

1. **Terminal** — a closed conversation accepts nothing.
2. **Table lookup** — is `(state, trigger)` even a legal move?
3. **Authorization** — is this actor allowed to fire this trigger?
4. **Guard** — do the conversation's own facts permit it?

Authorization before the guard, because a guard may read facts an unauthorised
actor should not be able to probe for. Table lookup before authorization,
because "that is not a thing you can do here" is a cheaper and less informative
answer than "you are not allowed to do that".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from demo_command_center.state.states import DemoState, is_terminal
from demo_command_center.state.transitions import (
    Command,
    GuardContext,
    Transition,
    lookup,
    triggers_from,
)
from demo_command_center.state.triggers import USER_FORBIDDEN, Actor, Trigger


class TransitionRejected(Exception):
    """The move is not permitted. Carries a machine-readable reason.

    A single exception type with a `reason` rather than a hierarchy: every
    caller does the same thing with it (record, metric, do not send), and the
    reason is what an operator actually needs.
    """

    def __init__(self, reason: str, *, state: DemoState, trigger: Trigger) -> None:
        super().__init__(f"{state.value}/{trigger.value}: {reason}")
        self.reason = reason
        self.state = state
        self.trigger = trigger


class ConcurrencyConflict(Exception):
    """Someone else advanced this conversation while we were deciding.

    Raised by the repository, not here, but defined alongside the machine
    because it is part of the same contract: the caller must reload and retry,
    never force the write.
    """

    def __init__(self, conversation_ref: str, *, expected: int, actual: int) -> None:
        super().__init__(
            f"conversation {conversation_ref} moved from version {expected} to {actual}"
        )
        self.conversation_ref = conversation_ref
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """The persisted state of one conversation, as the machine sees it."""

    conversation_ref: str
    state: DemoState = DemoState.NEW
    #: Optimistic lock. Every successful write increments it.
    version: int = 0
    demo_id: str | None = None
    updated_at: datetime | None = None
    #: Facts guards read. Assembled by the orchestrator from the demo row,
    #: the hold, the payment order and the verifier — never from user text.
    facts: dict[str, Any] = field(default_factory=dict)

    def with_facts(self, **extra: Any) -> StateSnapshot:
        return StateSnapshot(
            conversation_ref=self.conversation_ref,
            state=self.state,
            version=self.version,
            demo_id=self.demo_id,
            updated_at=self.updated_at,
            facts={**self.facts, **extra},
        )


@dataclass(frozen=True, slots=True)
class TransitionResult:
    """What the orchestrator must now do. Advisory until it is persisted."""

    conversation_ref: str
    from_state: DemoState
    to_state: DemoState
    trigger: Trigger
    actor: Actor
    command: Command
    compensation: Command
    reason: str
    #: The version the write must assert. A mismatch is `ConcurrencyConflict`.
    expected_version: int

    @property
    def changed(self) -> bool:
        """False for a self-transition, e.g. requirement capture.

        Worth distinguishing: a self-transition still runs its command and is
        still audited, but it must not bump a "state changed" metric or an
        alert that watches for stalled conversations.
        """
        return self.from_state is not self.to_state


class StateMachine:
    """Stateless. One instance is safe to share across a whole container."""

    def fire(
        self,
        snapshot: StateSnapshot,
        trigger: Trigger,
        *,
        actor: Actor,
        facts: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """Evaluate one event. Returns what to do, or raises `TransitionRejected`."""
        state = snapshot.state

        if is_terminal(state):
            raise TransitionRejected("conversation_is_terminal", state=state, trigger=trigger)

        transition = lookup(state, trigger)
        if transition is None:
            raise TransitionRejected("illegal_transition", state=state, trigger=trigger)

        self._authorise(transition, actor, state=state, trigger=trigger)

        merged = {**snapshot.facts, **(facts or {})}
        if transition.guard is not None:
            refusal = transition.guard(GuardContext(state=state, actor=actor, facts=merged))
            if refusal is not None:
                raise TransitionRejected(refusal, state=state, trigger=trigger)

        return TransitionResult(
            conversation_ref=snapshot.conversation_ref,
            from_state=state,
            to_state=transition.target,
            trigger=trigger,
            actor=actor,
            command=transition.command,
            compensation=transition.compensation,
            reason=transition.reason or trigger.value,
            expected_version=snapshot.version,
        )

    def can_fire(
        self,
        snapshot: StateSnapshot,
        trigger: Trigger,
        *,
        actor: Actor,
        facts: dict[str, Any] | None = None,
    ) -> bool:
        """Non-raising probe. Used to filter what an LLM is allowed to propose."""
        try:
            self.fire(snapshot, trigger, actor=actor, facts=facts)
        except TransitionRejected:
            return False
        return True

    def available(self, snapshot: StateSnapshot, *, actor: Actor) -> frozenset[Trigger]:
        """Triggers this actor could fire from here, ignoring guards.

        Guards are excluded on purpose: this is used to build the LLM's menu of
        permitted intents, and a guard failure is a *reason to explain*, not a
        reason to hide the option and leave the parent with no path forward.
        """
        out = set()
        for trigger in triggers_from(snapshot.state):
            transition = lookup(snapshot.state, trigger)
            if transition is None:
                continue
            try:
                self._authorise(transition, actor, state=snapshot.state, trigger=trigger)
            except TransitionRejected:
                continue
            out.add(trigger)
        return frozenset(out)

    # ------------------------------------------------------------- internals
    @staticmethod
    def _authorise(
        transition: Transition, actor: Actor, *, state: DemoState, trigger: Trigger
    ) -> None:
        """Two independent checks, both required.

        The `USER_FORBIDDEN` check is not redundant with the per-transition
        actor set: it is a global backstop, so adding `Actor.USER` to a
        commercial transition by mistake fails here as well as in the table
        invariants test.
        """
        if actor is Actor.USER and trigger in USER_FORBIDDEN:
            raise TransitionRejected("trigger_forbidden_for_user", state=state, trigger=trigger)
        if actor not in transition.actors:
            raise TransitionRejected(
                f"actor_not_permitted:{actor.value}", state=state, trigger=trigger
            )

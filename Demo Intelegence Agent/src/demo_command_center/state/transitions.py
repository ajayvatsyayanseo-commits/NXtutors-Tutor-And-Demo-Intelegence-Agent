"""The transition table. One row per legal move, and nothing else is legal.

A table rather than a graph of `if` statements, because the properties that make
this safe are properties *of the table* and can therefore be tested directly:
every state is reachable, no transition is authorised for `Actor.USER` that
appears in `USER_FORBIDDEN`, and every non-terminal state has an escape hatch to
`HUMAN_HANDOFF`.

Each row declares:

* `sources`      — allowed origin states. A trigger fired from anywhere else is
                   rejected, not silently ignored.
* `trigger`      — the event.
* `actors`       — who may fire it. This is the authorization check.
* `target`       — resulting state.
* `guard`        — an optional predicate over the conversation's own data. Guards
                   may only *refuse*; they can never redirect to a different
                   target, which keeps the table the single source of truth.
* `command`      — the side effect the orchestrator must perform afterwards.
                   Declared here, executed there: a transition that both mutates
                   state and calls Google is untestable and unrecoverable.
* `compensation` — what to run if `command` fails after the state moved.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import USER_FORBIDDEN, Actor, Trigger


class Command(StrEnum):
    """The side effect a transition asks the orchestrator to perform.

    Named, not a callable, so a transition row stays pure data — serialisable,
    diffable and assertable in a test without importing the whole capability
    layer.
    """

    NONE = "none"
    RESOLVE_IDENTITY = "resolve_identity"
    ASK_MISSING_REQUIREMENTS = "ask_missing_requirements"
    REQUEST_TUTOR_MATCH = "request_tutor_match"
    PRESENT_TUTOR_OPTIONS = "present_tutor_options"
    REQUEST_TUTOR_CONFIRMATION = "request_tutor_confirmation"
    PROPOSE_SLOTS = "propose_slots"
    PLACE_SLOT_HOLD = "place_slot_hold"
    RELEASE_SLOT_HOLD = "release_slot_hold"
    CREATE_CALENDAR_EVENT = "create_calendar_event"
    CANCEL_CALENDAR_EVENT = "cancel_calendar_event"
    SEND_CONFIRMATION = "send_confirmation"
    SCHEDULE_REMINDERS = "schedule_reminders"
    CANCEL_REMINDERS = "cancel_reminders"
    CAPTURE_OUTCOME = "capture_outcome"
    RUN_POST_DEMO_ANALYSIS = "run_post_demo_analysis"
    SEND_FOLLOWUP = "send_followup"
    CREATE_PAYMENT_ORDER = "create_payment_order"
    ACTIVATE_SUBSCRIPTION = "activate_subscription"
    HANDOFF_TO_ONBOARDING = "handoff_to_onboarding"
    SEND_WELCOME = "send_welcome"
    RAISE_HUMAN_CASE = "raise_human_case"
    NOTIFY_CANCELLATION = "notify_cancellation"
    TRY_FALLBACK_TUTOR = "try_fallback_tutor"


#: A guard reads the conversation's data and returns None to allow, or a reason
#: string to refuse. Returning a reason is not an error — it is a business rule.
Guard = Callable[["GuardContext"], str | None]


@dataclass(frozen=True, slots=True)
class GuardContext:
    """What a guard is allowed to see.

    Deliberately narrow. A guard that could reach the database would make the
    transition table's behaviour depend on I/O ordering, and the concurrency
    tests would stop meaning anything.
    """

    state: DemoState
    actor: Actor
    #: Conversation-scoped facts assembled by the orchestrator before the call.
    facts: dict[str, Any] = field(default_factory=dict)

    def fact(self, name: str, default: Any = None) -> Any:
        return self.facts.get(name, default)


@dataclass(frozen=True, slots=True)
class Transition:
    sources: frozenset[DemoState]
    trigger: Trigger
    actors: frozenset[Actor]
    target: DemoState
    command: Command = Command.NONE
    guard: Guard | None = None
    compensation: Command = Command.NONE
    #: Audit label. Written to `dcc_state_transitions.reason` verbatim.
    reason: str = ""


# ------------------------------------------------------------------- guards


def _require(fact: str, message: str) -> Guard:
    """A guard that refuses unless `fact` is truthy."""

    def check(ctx: GuardContext) -> str | None:
        return None if ctx.fact(fact) else message

    return check


def _requirements_complete(ctx: GuardContext) -> str | None:
    missing = ctx.fact("missing_requirements", ())
    return None if not missing else f"missing_requirements:{','.join(missing)}"


def _tutor_from_trusted_source(ctx: GuardContext) -> str | None:
    """The chosen tutor must have come from a candidate snapshot we stored.

    This is the anti-tampering rule in executable form: a `tutor_ref` that
    appears in a user message, or in LLM output, and is not in the snapshot we
    persisted when we presented the options, is refused here. Without it, a
    parent could name any tutor id and we would try to book them.
    """
    if not ctx.fact("tutor_ref"):
        return "no_tutor_selected"
    if not ctx.fact("tutor_in_snapshot"):
        return "tutor_not_in_presented_candidates"
    return None


def _hold_is_live(ctx: GuardContext) -> str | None:
    if not ctx.fact("hold_id"):
        return "no_slot_hold"
    if ctx.fact("hold_expired", False):
        return "slot_hold_expired"
    return None


def _payment_verified(ctx: GuardContext) -> str | None:
    """Only a verified, amount-matched provider event may pass.

    Three separate facts, all set by the webhook verifier, none of them derived
    from anything a customer said.
    """
    if not ctx.fact("signature_verified"):
        return "payment_signature_unverified"
    if not ctx.fact("amount_matches_order"):
        return "payment_amount_mismatch"
    if not ctx.fact("order_belongs_to_conversation"):
        return "payment_order_mismatch"
    return None


def _outcome_is_evidenced(ctx: GuardContext) -> str | None:
    """A no-show needs the grace period *and* a real absence signal.

    An LLM guess is explicitly not enough — `evidence_source` is set by the
    calendar/attendance capability, never by the extraction prompt.
    """
    if not ctx.fact("grace_period_elapsed"):
        return "grace_period_not_elapsed"
    source = ctx.fact("evidence_source")
    if source in (None, "", "llm"):
        return "no_authoritative_absence_evidence"
    return None


# -------------------------------------------------------------------- table

_SYSTEM = frozenset({Actor.SYSTEM})
_USER = frozenset({Actor.USER})
_USER_OR_SYSTEM = frozenset({Actor.USER, Actor.SYSTEM})
_ANY_HUMAN = frozenset({Actor.USER, Actor.OPERATOR})
_OPERATOR = frozenset({Actor.OPERATOR})
_SCHEDULER = frozenset({Actor.SCHEDULER, Actor.SYSTEM})

#: Every state a human can be pulled into the conversation from. Generated
#: rather than listed so a new state cannot be added without an escape hatch.
_ESCALATABLE = frozenset(
    state
    for state in DemoState
    if state
    not in {
        DemoState.HUMAN_HANDOFF,
        DemoState.CONVERTED,
        DemoState.CANCELLED,
        DemoState.FAILED_TERMINAL,
    }
)

#: Non-terminal states a user may abandon from.
_CANCELLABLE = frozenset(
    state
    for state in DemoState
    if state
    not in {
        DemoState.CONVERTED,
        DemoState.CANCELLED,
        DemoState.FAILED_TERMINAL,
        DemoState.PAYMENT_CONFIRMED,
        DemoState.SUBSCRIPTION_ACTIVATING,
    }
)


TRANSITIONS: tuple[Transition, ...] = (
    # ------------------------------------------------------------- intake
    Transition(
        sources=frozenset({DemoState.NEW}),
        trigger=Trigger.HANDOFF_RECEIVED,
        actors=frozenset({Actor.AGENT, Actor.SYSTEM}),
        target=DemoState.OWNERSHIP_ACQUIRING,
        reason="handoff_accepted",
    ),
    Transition(
        sources=frozenset({DemoState.OWNERSHIP_ACQUIRING}),
        trigger=Trigger.OWNERSHIP_ACQUIRED,
        actors=_SYSTEM,
        target=DemoState.IDENTITY_RESOLUTION,
        command=Command.RESOLVE_IDENTITY,
        reason="ownership_taken",
    ),
    Transition(
        sources=frozenset({DemoState.IDENTITY_RESOLUTION}),
        trigger=Trigger.IDENTITY_RESOLVED,
        actors=_SYSTEM,
        target=DemoState.COLLECTING_REQUIREMENTS,
        command=Command.ASK_MISSING_REQUIREMENTS,
        reason="identity_resolved",
    ),
    Transition(
        # An unresolved identity is not a failure: a parent with no website
        # account can still book a demo. We simply proceed with a pending ref.
        sources=frozenset({DemoState.IDENTITY_RESOLUTION}),
        trigger=Trigger.IDENTITY_UNRESOLVED,
        actors=_SYSTEM,
        target=DemoState.COLLECTING_REQUIREMENTS,
        command=Command.ASK_MISSING_REQUIREMENTS,
        reason="identity_pending",
    ),
    Transition(
        sources=frozenset({DemoState.COLLECTING_REQUIREMENTS}),
        trigger=Trigger.REQUIREMENTS_UPDATED,
        actors=_USER_OR_SYSTEM,
        target=DemoState.COLLECTING_REQUIREMENTS,
        command=Command.ASK_MISSING_REQUIREMENTS,
        reason="requirement_captured",
    ),
    Transition(
        sources=frozenset({DemoState.COLLECTING_REQUIREMENTS}),
        trigger=Trigger.REQUIREMENTS_COMPLETE,
        actors=_SYSTEM,
        target=DemoState.TUTOR_MATCH_REQUESTED,
        command=Command.REQUEST_TUTOR_MATCH,
        guard=_requirements_complete,
        reason="requirements_complete",
    ),
    # ---------------------------------------------------- tutor discovery
    Transition(
        sources=frozenset({DemoState.TUTOR_MATCH_REQUESTED}),
        trigger=Trigger.MATCH_SUCCEEDED,
        actors=_SYSTEM,
        target=DemoState.TUTOR_OPTIONS_READY,
        command=Command.PRESENT_TUTOR_OPTIONS,
        guard=_require("has_candidates", "no_candidates_returned"),
        reason="candidates_returned",
    ),
    Transition(
        sources=frozenset({DemoState.TUTOR_MATCH_REQUESTED}),
        trigger=Trigger.MATCH_EMPTY,
        actors=_SYSTEM,
        target=DemoState.HUMAN_HANDOFF,
        command=Command.RAISE_HUMAN_CASE,
        reason="no_tutor_available",
    ),
    Transition(
        sources=frozenset({DemoState.TUTOR_OPTIONS_READY}),
        trigger=Trigger.OPTIONS_PRESENTED,
        actors=_SYSTEM,
        target=DemoState.AWAITING_TUTOR_SELECTION,
        reason="options_sent",
    ),
    Transition(
        sources=frozenset({DemoState.AWAITING_TUTOR_SELECTION}),
        trigger=Trigger.TUTOR_CHOSEN,
        actors=_USER_OR_SYSTEM,
        target=DemoState.TUTOR_SELECTED,
        command=Command.PROPOSE_SLOTS,
        guard=_tutor_from_trusted_source,
        reason="tutor_selected",
    ),
    Transition(
        sources=frozenset({DemoState.AWAITING_TUTOR_SELECTION}),
        trigger=Trigger.OPTIONS_REJECTED,
        actors=_USER_OR_SYSTEM,
        target=DemoState.TUTOR_MATCH_REQUESTED,
        command=Command.REQUEST_TUTOR_MATCH,
        reason="alternatives_requested",
    ),
    # ------------------------------------------------ slot and confirmation
    Transition(
        sources=frozenset({DemoState.TUTOR_SELECTED, DemoState.NEGOTIATING_SLOT}),
        trigger=Trigger.SLOT_PROPOSED,
        actors=_SYSTEM,
        target=DemoState.NEGOTIATING_SLOT,
        reason="slots_offered",
    ),
    Transition(
        sources=frozenset({DemoState.NEGOTIATING_SLOT, DemoState.TUTOR_SELECTED}),
        trigger=Trigger.SLOT_AGREED,
        actors=_USER_OR_SYSTEM,
        target=DemoState.SLOT_HELD,
        command=Command.PLACE_SLOT_HOLD,
        reason="slot_agreed",
    ),
    Transition(
        sources=frozenset({DemoState.SLOT_HELD}),
        trigger=Trigger.HOLD_PLACED,
        actors=_SYSTEM,
        target=DemoState.TUTOR_CONFIRMATION_PENDING,
        command=Command.REQUEST_TUTOR_CONFIRMATION,
        guard=_hold_is_live,
        reason="hold_placed",
    ),
    Transition(
        sources=frozenset({DemoState.SLOT_HELD, DemoState.TUTOR_CONFIRMATION_PENDING}),
        trigger=Trigger.HOLD_EXPIRED,
        actors=_SCHEDULER,
        target=DemoState.NEGOTIATING_SLOT,
        command=Command.PROPOSE_SLOTS,
        compensation=Command.RELEASE_SLOT_HOLD,
        reason="hold_expired",
    ),
    Transition(
        sources=frozenset({DemoState.TUTOR_CONFIRMATION_PENDING}),
        trigger=Trigger.TUTOR_ACCEPTED,
        actors=frozenset({Actor.TUTOR, Actor.OPERATOR, Actor.SYSTEM}),
        target=DemoState.CALENDAR_CREATION_PENDING,
        command=Command.CREATE_CALENDAR_EVENT,
        guard=_hold_is_live,
        # If Google fails here the hold must go back, or the slot stays blocked
        # for its full TTL after a failure nobody can see. The compensation
        # belongs on *this* transition because this is the one whose command
        # took the hold into calendar creation.
        compensation=Command.RELEASE_SLOT_HOLD,
        reason="tutor_accepted",
    ),
    Transition(
        sources=frozenset({DemoState.TUTOR_CONFIRMATION_PENDING}),
        trigger=Trigger.TUTOR_DECLINED,
        actors=frozenset({Actor.TUTOR, Actor.OPERATOR}),
        target=DemoState.TUTOR_MATCH_REQUESTED,
        command=Command.TRY_FALLBACK_TUTOR,
        compensation=Command.RELEASE_SLOT_HOLD,
        reason="tutor_declined",
    ),
    Transition(
        sources=frozenset({DemoState.TUTOR_CONFIRMATION_PENDING}),
        trigger=Trigger.TUTOR_REQUEST_EXPIRED,
        actors=_SCHEDULER,
        target=DemoState.TUTOR_MATCH_REQUESTED,
        command=Command.TRY_FALLBACK_TUTOR,
        compensation=Command.RELEASE_SLOT_HOLD,
        reason="tutor_request_expired",
    ),
    # ------------------------------------------------------------ calendar
    Transition(
        sources=frozenset({DemoState.CALENDAR_CREATION_PENDING}),
        trigger=Trigger.CALENDAR_CREATED,
        actors=_SYSTEM,
        target=DemoState.SCHEDULED,
        command=Command.SEND_CONFIRMATION,
        guard=_require("calendar_event_id", "calendar_event_missing"),
        reason="calendar_created",
    ),
    Transition(
        # Compensation matters here: the hold must be released or the slot is
        # blocked for its full TTL after a Google failure.
        sources=frozenset({DemoState.CALENDAR_CREATION_PENDING}),
        trigger=Trigger.CALENDAR_FAILED,
        actors=_SYSTEM,
        target=DemoState.FAILED_RECOVERABLE,
        compensation=Command.RELEASE_SLOT_HOLD,
        reason="calendar_failed",
    ),
    Transition(
        sources=frozenset({DemoState.FAILED_RECOVERABLE}),
        trigger=Trigger.RETRY,
        actors=frozenset({Actor.SYSTEM, Actor.OPERATOR, Actor.SCHEDULER}),
        target=DemoState.NEGOTIATING_SLOT,
        command=Command.PROPOSE_SLOTS,
        reason="retry_after_failure",
    ),
    Transition(
        sources=frozenset({DemoState.SCHEDULED}),
        trigger=Trigger.REMINDERS_SCHEDULED,
        actors=_SYSTEM,
        target=DemoState.REMINDERS_ACTIVE,
        command=Command.SCHEDULE_REMINDERS,
        reason="reminders_scheduled",
    ),
    Transition(
        sources=frozenset({DemoState.REMINDERS_ACTIVE, DemoState.SCHEDULED}),
        trigger=Trigger.DEMO_WINDOW_OPEN,
        actors=_SCHEDULER,
        target=DemoState.DEMO_READY,
        reason="demo_window_open",
    ),
    # ------------------------------------------------ reschedule and cancel
    Transition(
        sources=frozenset(
            {
                DemoState.SCHEDULED,
                DemoState.REMINDERS_ACTIVE,
                DemoState.DEMO_READY,
                DemoState.SLOT_HELD,
            }
        ),
        trigger=Trigger.RESCHEDULE_REQUESTED,
        actors=frozenset({Actor.USER, Actor.TUTOR, Actor.OPERATOR}),
        target=DemoState.NEGOTIATING_SLOT,
        command=Command.PROPOSE_SLOTS,
        compensation=Command.CANCEL_REMINDERS,
        reason="reschedule_requested",
    ),
    Transition(
        sources=_CANCELLABLE,
        trigger=Trigger.CANCELLED_BY_USER,
        actors=_ANY_HUMAN,
        target=DemoState.CANCELLED,
        command=Command.NOTIFY_CANCELLATION,
        compensation=Command.CANCEL_CALENDAR_EVENT,
        reason="cancelled_by_user",
    ),
    Transition(
        sources=frozenset({DemoState.SCHEDULED, DemoState.REMINDERS_ACTIVE, DemoState.DEMO_READY}),
        trigger=Trigger.CANCELLED_BY_TUTOR,
        actors=frozenset({Actor.TUTOR, Actor.OPERATOR}),
        target=DemoState.NEGOTIATING_SLOT,
        command=Command.TRY_FALLBACK_TUTOR,
        compensation=Command.CANCEL_CALENDAR_EVENT,
        reason="cancelled_by_tutor",
    ),
    # ------------------------------------------------------------- outcome
    Transition(
        sources=frozenset({DemoState.DEMO_READY, DemoState.REMINDERS_ACTIVE}),
        trigger=Trigger.OUTCOME_DUE,
        actors=_SCHEDULER,
        target=DemoState.OUTCOME_PENDING,
        command=Command.CAPTURE_OUTCOME,
        reason="outcome_due",
    ),
    Transition(
        sources=frozenset({DemoState.OUTCOME_PENDING, DemoState.DEMO_READY}),
        trigger=Trigger.DEMO_COMPLETED,
        actors=frozenset({Actor.SYSTEM, Actor.OPERATOR, Actor.TUTOR}),
        target=DemoState.COMPLETED,
        command=Command.RUN_POST_DEMO_ANALYSIS,
        reason="demo_completed",
    ),
    Transition(
        sources=frozenset({DemoState.OUTCOME_PENDING, DemoState.DEMO_READY}),
        trigger=Trigger.STUDENT_ABSENT,
        actors=frozenset({Actor.SYSTEM, Actor.OPERATOR}),
        target=DemoState.STUDENT_NO_SHOW_REVIEW,
        guard=_outcome_is_evidenced,
        reason="student_no_show",
    ),
    Transition(
        sources=frozenset({DemoState.OUTCOME_PENDING, DemoState.DEMO_READY}),
        trigger=Trigger.TUTOR_ABSENT,
        actors=frozenset({Actor.SYSTEM, Actor.OPERATOR}),
        target=DemoState.TUTOR_NO_SHOW_REVIEW,
        guard=_outcome_is_evidenced,
        reason="tutor_no_show",
    ),
    Transition(
        sources=frozenset({DemoState.STUDENT_NO_SHOW_REVIEW, DemoState.TUTOR_NO_SHOW_REVIEW}),
        trigger=Trigger.NO_SHOW_RESOLVED,
        actors=frozenset({Actor.OPERATOR, Actor.SYSTEM}),
        target=DemoState.NEGOTIATING_SLOT,
        command=Command.PROPOSE_SLOTS,
        reason="no_show_rebooking",
    ),
    # ----------------------------------------------------------- post-demo
    Transition(
        sources=frozenset({DemoState.COMPLETED}),
        trigger=Trigger.ANALYSIS_REQUESTED,
        actors=_SYSTEM,
        target=DemoState.POST_DEMO_ANALYSIS,
        command=Command.RUN_POST_DEMO_ANALYSIS,
        reason="analysis_started",
    ),
    Transition(
        sources=frozenset({DemoState.POST_DEMO_ANALYSIS, DemoState.COMPLETED}),
        trigger=Trigger.ANALYSIS_COMPLETE,
        actors=_SYSTEM,
        target=DemoState.FOLLOWUP_PENDING,
        command=Command.SEND_FOLLOWUP,
        reason="analysis_complete",
    ),
    Transition(
        sources=frozenset({DemoState.FOLLOWUP_PENDING}),
        trigger=Trigger.FOLLOWUP_SENT,
        actors=_SYSTEM,
        target=DemoState.FOLLOWUP_PENDING,
        reason="followup_sent",
    ),
    # ---------------------------------------------------------- commercial
    Transition(
        sources=frozenset({DemoState.FOLLOWUP_PENDING, DemoState.POST_DEMO_ANALYSIS}),
        trigger=Trigger.PAYMENT_LINK_ISSUED,
        actors=_SYSTEM,
        target=DemoState.PAYMENT_PENDING,
        command=Command.CREATE_PAYMENT_ORDER,
        guard=_require("approved_quote", "no_approved_quote"),
        reason="payment_link_issued",
    ),
    Transition(
        sources=frozenset({DemoState.PAYMENT_PENDING}),
        trigger=Trigger.PAYMENT_PAID,
        actors=frozenset({Actor.PAYMENT_PROVIDER}),
        target=DemoState.PAYMENT_CONFIRMED,
        command=Command.ACTIVATE_SUBSCRIPTION,
        guard=_payment_verified,
        reason="payment_verified",
    ),
    Transition(
        sources=frozenset({DemoState.PAYMENT_PENDING}),
        trigger=Trigger.PAYMENT_FAILED,
        actors=frozenset({Actor.PAYMENT_PROVIDER}),
        target=DemoState.FOLLOWUP_PENDING,
        reason="payment_failed",
    ),
    Transition(
        sources=frozenset({DemoState.PAYMENT_CONFIRMED}),
        trigger=Trigger.ACTIVATION_STARTED,
        actors=_SYSTEM,
        target=DemoState.SUBSCRIPTION_ACTIVATING,
        command=Command.ACTIVATE_SUBSCRIPTION,
        reason="activation_started",
    ),
    Transition(
        sources=frozenset({DemoState.SUBSCRIPTION_ACTIVATING, DemoState.PAYMENT_CONFIRMED}),
        trigger=Trigger.ACTIVATION_SUCCEEDED,
        actors=_SYSTEM,
        target=DemoState.ONBOARDING_HANDOFF_PENDING,
        command=Command.HANDOFF_TO_ONBOARDING,
        guard=_require("subscription_ref", "no_subscription_ref"),
        reason="subscription_activated",
    ),
    Transition(
        # Money is taken but activation failed. Never terminal, never silent: a
        # human must close this, so it goes to the review queue.
        sources=frozenset({DemoState.SUBSCRIPTION_ACTIVATING}),
        trigger=Trigger.ACTIVATION_FAILED,
        actors=_SYSTEM,
        target=DemoState.HUMAN_HANDOFF,
        command=Command.RAISE_HUMAN_CASE,
        reason="activation_failed_paid",
    ),
    Transition(
        sources=frozenset({DemoState.ONBOARDING_HANDOFF_PENDING}),
        trigger=Trigger.ONBOARDING_ACCEPTED,
        actors=frozenset({Actor.AGENT, Actor.SYSTEM}),
        target=DemoState.CONVERTED,
        command=Command.SEND_WELCOME,
        reason="onboarding_accepted",
    ),
    # --------------------------------------------------------- exceptional
    Transition(
        sources=_ESCALATABLE,
        trigger=Trigger.HUMAN_REQUESTED,
        actors=frozenset({Actor.USER, Actor.SYSTEM, Actor.OPERATOR}),
        target=DemoState.HUMAN_HANDOFF,
        command=Command.RAISE_HUMAN_CASE,
        reason="human_requested",
    ),
    Transition(
        sources=frozenset({DemoState.HUMAN_HANDOFF}),
        trigger=Trigger.HUMAN_RESOLVED,
        actors=_OPERATOR,
        target=DemoState.COLLECTING_REQUIREMENTS,
        command=Command.ASK_MISSING_REQUIREMENTS,
        reason="human_resolved",
    ),
    Transition(
        sources=_ESCALATABLE,
        trigger=Trigger.RECOVERABLE_FAILURE,
        actors=_SYSTEM,
        target=DemoState.FAILED_RECOVERABLE,
        reason="recoverable_failure",
    ),
    Transition(
        sources=frozenset({DemoState.FAILED_RECOVERABLE}),
        trigger=Trigger.ABANDON,
        actors=frozenset({Actor.OPERATOR, Actor.SYSTEM}),
        target=DemoState.FAILED_TERMINAL,
        reason="abandoned",
    ),
)


#: `(source, trigger) -> transition`. Built once; the machine does a dict lookup
#: rather than scanning the table on every event.
INDEX: dict[tuple[DemoState, Trigger], Transition] = {}
for _t in TRANSITIONS:
    for _source in _t.sources:
        key = (_source, _t.trigger)
        if key in INDEX:  # pragma: no cover - a table bug, caught by the tests
            raise RuntimeError(f"ambiguous transition for {_source.value}/{_t.trigger.value}")
        INDEX[key] = _t


def lookup(state: DemoState, trigger: Trigger) -> Transition | None:
    return INDEX.get((state, trigger))


def triggers_from(state: DemoState) -> frozenset[Trigger]:
    return frozenset(trigger for (source, trigger) in INDEX if source is state)


def user_authorised_triggers() -> frozenset[Trigger]:
    return frozenset(t.trigger for t in TRANSITIONS if Actor.USER in t.actors)


def table_invariants() -> list[str]:
    """Structural problems with the table itself. Must be empty.

    Run by the unit tests and by `dcc-doctor`, so a bad table fails a check
    rather than one specific conversation at 2am.
    """
    problems: list[str] = []

    forbidden = user_authorised_triggers() & USER_FORBIDDEN
    if forbidden:
        problems.append(f"user-authorised triggers in USER_FORBIDDEN: {sorted(forbidden)}")

    reachable = {DemoState.NEW} | {t.target for t in TRANSITIONS}
    unreachable = sorted(s.value for s in DemoState if s not in reachable)
    if unreachable:
        problems.append(f"unreachable states: {unreachable}")

    from demo_command_center.state.states import TERMINAL

    for state in DemoState:
        if state in TERMINAL or state is DemoState.HUMAN_HANDOFF:
            continue
        if not triggers_from(state):
            problems.append(f"dead-end state with no outgoing transition: {state.value}")

    return problems

"""The state machine's safety properties, tested directly.

Pure tests — no repositories, no clock, no I/O. That is only possible because
guards read a `facts` dict the orchestrator assembles, and it is what lets these
tests construct scenarios (an expired hold, an unverified payment) that would be
hard to arrange against a database.
"""

from __future__ import annotations

import pytest

from demo_command_center.state.machine import (
    StateMachine,
    StateSnapshot,
    TransitionRejected,
)
from demo_command_center.state.states import TERMINAL, DemoState
from demo_command_center.state.transitions import (
    INDEX,
    TRANSITIONS,
    Command,
    table_invariants,
)
from demo_command_center.state.triggers import USER_FORBIDDEN, Actor, Trigger


@pytest.fixture
def machine() -> StateMachine:
    return StateMachine()


def snapshot(state: DemoState, **facts: object) -> StateSnapshot:
    return StateSnapshot(conversation_ref="cv_x", state=state, version=1, facts=dict(facts))


# ------------------------------------------------------------ table shape


def test_transition_table_has_no_structural_problems() -> None:
    """Every state reachable, no dead ends, no user-forbidden trigger exposed."""
    assert table_invariants() == []


def test_no_user_authorised_transition_fires_a_forbidden_trigger() -> None:
    """The global backstop, independent of each transition's actor set.

    A future edit that adds `Actor.USER` to the payment transition fails here as
    well as in `table_invariants`, so the property survives a refactor of either.
    """
    offenders = [
        t.trigger.value
        for t in TRANSITIONS
        if Actor.USER in t.actors and t.trigger in USER_FORBIDDEN
    ]
    assert offenders == []


def test_every_source_trigger_pair_is_unambiguous() -> None:
    """`INDEX` is built at import and raises on a duplicate; assert it is full."""
    pairs = sum(len(t.sources) for t in TRANSITIONS)
    assert len(INDEX) == pairs


# ------------------------------------------------------- illegal transitions


def test_illegal_transition_is_rejected(machine: StateMachine) -> None:
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(snapshot(DemoState.NEW), Trigger.PAYMENT_PAID, actor=Actor.PAYMENT_PROVIDER)
    assert exc.value.reason == "illegal_transition"


@pytest.mark.parametrize("state", sorted(TERMINAL, key=lambda s: s.value))
def test_terminal_states_accept_nothing(machine: StateMachine, state: DemoState) -> None:
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(snapshot(state), Trigger.HUMAN_REQUESTED, actor=Actor.OPERATOR)
    assert exc.value.reason == "conversation_is_terminal"


def test_user_cannot_declare_payment(machine: StateMachine) -> None:
    """ "I have paid" is a claim, not an event. The actor check is the control."""
    state = snapshot(
        DemoState.PAYMENT_PENDING,
        signature_verified=True,
        amount_matches_order=True,
        order_belongs_to_conversation=True,
    )
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(state, Trigger.PAYMENT_PAID, actor=Actor.USER)
    assert exc.value.reason == "trigger_forbidden_for_user"


def test_user_cannot_declare_the_demo_completed(machine: StateMachine) -> None:
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(snapshot(DemoState.OUTCOME_PENDING), Trigger.DEMO_COMPLETED, actor=Actor.USER)
    assert exc.value.reason == "trigger_forbidden_for_user"


# -------------------------------------------------------------------- guards


def test_tutor_selection_must_come_from_the_stored_snapshot(machine: StateMachine) -> None:
    """A tutor ref the parent supplied but we never presented is refused."""
    state = snapshot(
        DemoState.AWAITING_TUTOR_SELECTION, tutor_ref="tut_injected", tutor_in_snapshot=False
    )
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(state, Trigger.TUTOR_CHOSEN, actor=Actor.USER)
    assert exc.value.reason == "tutor_not_in_presented_candidates"


def test_tutor_selection_from_snapshot_is_allowed(machine: StateMachine) -> None:
    state = snapshot(
        DemoState.AWAITING_TUTOR_SELECTION, tutor_ref="tut_real", tutor_in_snapshot=True
    )
    result = machine.fire(state, Trigger.TUTOR_CHOSEN, actor=Actor.USER)
    assert result.to_state is DemoState.TUTOR_SELECTED
    assert result.command is Command.PROPOSE_SLOTS


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        ({"signature_verified": False}, "payment_signature_unverified"),
        ({"signature_verified": True, "amount_matches_order": False}, "payment_amount_mismatch"),
        (
            {
                "signature_verified": True,
                "amount_matches_order": True,
                "order_belongs_to_conversation": False,
            },
            "payment_order_mismatch",
        ),
    ],
)
def test_payment_guard_refuses_each_failure_independently(
    machine: StateMachine, facts: dict[str, object], reason: str
) -> None:
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(
            snapshot(DemoState.PAYMENT_PENDING, **facts),
            Trigger.PAYMENT_PAID,
            actor=Actor.PAYMENT_PROVIDER,
        )
    assert exc.value.reason == reason


def test_no_show_cannot_rest_on_an_llm_guess(machine: StateMachine) -> None:
    state = snapshot(DemoState.OUTCOME_PENDING, grace_period_elapsed=True, evidence_source="llm")
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(state, Trigger.STUDENT_ABSENT, actor=Actor.SYSTEM)
    assert exc.value.reason == "no_authoritative_absence_evidence"


def test_no_show_needs_the_grace_period(machine: StateMachine) -> None:
    state = snapshot(
        DemoState.OUTCOME_PENDING,
        grace_period_elapsed=False,
        evidence_source="meet_participation",
    )
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(state, Trigger.TUTOR_ABSENT, actor=Actor.SYSTEM)
    assert exc.value.reason == "grace_period_not_elapsed"


def test_no_show_with_authoritative_evidence_is_allowed(machine: StateMachine) -> None:
    state = snapshot(
        DemoState.OUTCOME_PENDING,
        grace_period_elapsed=True,
        evidence_source="meet_participation",
    )
    assert (
        machine.fire(state, Trigger.STUDENT_ABSENT, actor=Actor.SYSTEM).to_state
        is DemoState.STUDENT_NO_SHOW_REVIEW
    )


def test_expired_hold_blocks_the_calendar(machine: StateMachine) -> None:
    state = snapshot(DemoState.TUTOR_CONFIRMATION_PENDING, hold_id="hld_1", hold_expired=True)
    with pytest.raises(TransitionRejected) as exc:
        machine.fire(state, Trigger.TUTOR_ACCEPTED, actor=Actor.TUTOR)
    assert exc.value.reason == "slot_hold_expired"


# ---------------------------------------------------------------- behaviour


def test_transition_carries_its_compensation(machine: StateMachine) -> None:
    """A Google failure must declare how to undo the hold, not just fail."""
    result = machine.fire(
        snapshot(DemoState.CALENDAR_CREATION_PENDING), Trigger.CALENDAR_FAILED, actor=Actor.SYSTEM
    )
    assert result.to_state is DemoState.FAILED_RECOVERABLE
    assert result.compensation is Command.RELEASE_SLOT_HOLD


def test_available_lists_only_triggers_this_actor_may_fire(machine: StateMachine) -> None:
    state = snapshot(DemoState.PAYMENT_PENDING)
    assert Trigger.PAYMENT_PAID not in machine.available(state, actor=Actor.USER)
    assert Trigger.PAYMENT_PAID in machine.available(state, actor=Actor.PAYMENT_PROVIDER)


def test_self_transition_is_reported_as_unchanged(machine: StateMachine) -> None:
    result = machine.fire(
        snapshot(DemoState.COLLECTING_REQUIREMENTS),
        Trigger.REQUIREMENTS_UPDATED,
        actor=Actor.USER,
    )
    assert result.to_state is DemoState.COLLECTING_REQUIREMENTS
    assert result.changed is False


def test_expected_version_is_carried_for_the_optimistic_lock(machine: StateMachine) -> None:
    state = StateSnapshot(conversation_ref="cv_x", state=DemoState.NEW, version=7)
    result = machine.fire(state, Trigger.HANDOFF_RECEIVED, actor=Actor.AGENT)
    assert result.expected_version == 7

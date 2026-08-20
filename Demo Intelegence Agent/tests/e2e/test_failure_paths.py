"""Concurrency, contention and the paths where something goes wrong.

The happy path is covered by `test_lifecycle.py`. These are the cases that
decide whether the system is safe: two parents racing one slot, a tutor who
never answers, Google failing after the hold was taken, and a conversation a
human took over mid-flight.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from demo_command_center.contracts.common import DemoMode, Party
from demo_command_center.domain.demo import Demo, DemoAttendee
from demo_command_center.domain.messages import MessageKind, OutboundMessage, SendOutcome
from demo_command_center.domain.slots import SlotConflict, TimeSlot, new_hold
from demo_command_center.orchestration.context import Dependencies
from demo_command_center.orchestration.orchestrator import DemoCommandCenterOrchestrator
from demo_command_center.security.signatures import idempotency_key
from demo_command_center.shared.clock import FrozenClock
from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import Actor, Trigger

pytestmark = pytest.mark.e2e


async def advance_to_selection(orchestrator: DemoCommandCenterOrchestrator, ref: str) -> None:
    """Drive a conversation up to "a tutor has been chosen"."""
    steps = [
        (Trigger.HANDOFF_RECEIVED, Actor.AGENT, {}),
        (Trigger.OWNERSHIP_ACQUIRED, Actor.SYSTEM, {}),
        (Trigger.IDENTITY_RESOLVED, Actor.SYSTEM, {"phone_hash": "ph_x"}),
        (Trigger.REQUIREMENTS_COMPLETE, Actor.SYSTEM, {}),
        (Trigger.MATCH_SUCCEEDED, Actor.SYSTEM, {}),
        (Trigger.OPTIONS_PRESENTED, Actor.SYSTEM, {}),
        (Trigger.TUTOR_CHOSEN, Actor.USER, {"button_id": "tutor:1"}),
    ]
    for index, (trigger, actor, payload) in enumerate(steps):
        await orchestrator.handle(
            conversation_ref=ref,
            trigger=trigger,
            actor=actor,
            event_id=f"{ref}_evt_{index}",
            payload=payload,
        )


# =============================================================== slot races


async def test_two_conversations_cannot_hold_the_same_slot(
    deps: Dependencies, clock: FrozenClock
) -> None:
    """The exclusion is the unique index, not a read-then-write."""
    slot = TimeSlot(starts_at=clock.now() + timedelta(days=1, hours=8))
    first = new_hold(
        hold_id="hld_a",
        conversation_ref="cv_a",
        tutor_ref="tut_shared",
        slot=slot,
        mode=DemoMode.ONLINE,
        now=clock.now(),
    )
    second = new_hold(
        hold_id="hld_b",
        conversation_ref="cv_b",
        tutor_ref="tut_shared",
        slot=slot,
        mode=DemoMode.ONLINE,
        now=clock.now(),
    )
    await deps.slots.place_hold(first)
    with pytest.raises(SlotConflict) as exc:
        await deps.slots.place_hold(second)
    assert exc.value.existing_hold_id == "hld_a"


async def test_concurrent_holds_produce_exactly_one_winner(
    deps: Dependencies, clock: FrozenClock
) -> None:
    """Ten coroutines, one slot. Nine must lose."""
    slot = TimeSlot(starts_at=clock.now() + timedelta(days=2, hours=8))

    async def attempt(index: int) -> bool:
        hold = new_hold(
            hold_id=f"hld_{index}",
            conversation_ref=f"cv_{index}",
            tutor_ref="tut_contended",
            slot=slot,
            mode=DemoMode.ONLINE,
            now=clock.now(),
        )
        try:
            await deps.slots.place_hold(hold)
        except SlotConflict:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt(i) for i in range(10)))
    assert sum(outcomes) == 1, f"expected exactly one winner, got {sum(outcomes)}"


async def test_a_released_hold_frees_the_slot(deps: Dependencies, clock: FrozenClock) -> None:
    """A crashed negotiation must not block a tutor's evening forever."""
    slot = TimeSlot(starts_at=clock.now() + timedelta(days=3, hours=8))
    first = new_hold(
        hold_id="hld_r1",
        conversation_ref="cv_r1",
        tutor_ref="tut_free",
        slot=slot,
        mode=DemoMode.ONLINE,
        now=clock.now(),
    )
    await deps.slots.place_hold(first)
    await deps.slots.release("hld_r1", now=clock.now())

    second = new_hold(
        hold_id="hld_r2",
        conversation_ref="cv_r2",
        tutor_ref="tut_free",
        slot=slot,
        mode=DemoMode.ONLINE,
        now=clock.now(),
    )
    assert await deps.slots.place_hold(second) is second


async def test_holds_at_different_minutes_do_not_collide(
    deps: Dependencies, clock: FrozenClock
) -> None:
    base = clock.now() + timedelta(days=1, hours=8)
    for index, offset in enumerate((0, 60, 120)):
        await deps.slots.place_hold(
            new_hold(
                hold_id=f"hld_m{index}",
                conversation_ref=f"cv_m{index}",
                tutor_ref="tut_busy",
                slot=TimeSlot(starts_at=base + timedelta(minutes=offset)),
                mode=DemoMode.ONLINE,
                now=clock.now(),
            )
        )


# ==================================================== optimistic concurrency


async def test_two_concurrent_turns_do_not_both_advance_the_state(
    orchestrator: DemoCommandCenterOrchestrator, deps: Dependencies
) -> None:
    """Optimistic locking: one write wins, the loser reloads and re-decides."""
    results = await asyncio.gather(
        orchestrator.handle(
            conversation_ref="cv_race",
            trigger=Trigger.HANDOFF_RECEIVED,
            actor=Actor.AGENT,
            event_id="evt_race_1",
        ),
        orchestrator.handle(
            conversation_ref="cv_race",
            trigger=Trigger.HANDOFF_RECEIVED,
            actor=Actor.AGENT,
            event_id="evt_race_2",
        ),
    )
    accepted = [r for r in results if r.accepted and not r.duplicate]
    assert len(accepted) == 1, "two turns must not both apply the same transition"
    snapshot = await orchestrator.snapshot("cv_race")
    assert snapshot.version == 1


# ================================================== tutor decline and expiry


@pytest.mark.parametrize(
    ("trigger", "actor"),
    [(Trigger.TUTOR_DECLINED, Actor.TUTOR), (Trigger.TUTOR_REQUEST_EXPIRED, Actor.SCHEDULER)],
)
async def test_a_declined_or_expired_tutor_request_releases_the_hold_and_rematches(
    orchestrator: DemoCommandCenterOrchestrator,
    deps: Dependencies,
    seeded_request: Any,
    conversation_ref: str,
    trigger: Trigger,
    actor: Actor,
) -> None:
    await advance_to_selection(orchestrator, conversation_ref)
    slot = TimeSlot(starts_at=deps.clock.now() + timedelta(days=1, hours=8))
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.SLOT_AGREED,
        actor=Actor.USER,
        event_id="evt_slot",
        payload={"slot": slot},
    )
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.HOLD_PLACED,
        actor=Actor.SYSTEM,
        event_id="evt_hold",
    )

    result = await orchestrator.handle(
        conversation_ref=conversation_ref, trigger=trigger, actor=actor, event_id="evt_decline"
    )
    assert result.state == DemoState.TUTOR_MATCH_REQUESTED.value
    hold = await deps.slots.active_hold_for(conversation_ref, now=deps.clock.now())
    assert hold is None, "the compensation must release the hold"


# ================================================ provider failure handling


async def test_a_google_failure_compensates_the_hold_and_does_not_confirm(
    orchestrator: DemoCommandCenterOrchestrator,
    deps: Dependencies,
    doubles: dict[str, Any],
    seeded_request: Any,
    conversation_ref: str,
) -> None:
    """No calendar event means no confirmation, and the slot goes back."""
    await advance_to_selection(orchestrator, conversation_ref)
    slot = TimeSlot(starts_at=deps.clock.now() + timedelta(days=1, hours=8))
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.SLOT_AGREED,
        actor=Actor.USER,
        event_id="evt_slot",
        payload={"slot": slot},
    )
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.HOLD_PLACED,
        actor=Actor.SYSTEM,
        event_id="evt_hold",
    )

    doubles["calendar"].fail_create = True
    result = await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.TUTOR_ACCEPTED,
        actor=Actor.TUTOR,
        event_id="evt_accept",
    )

    assert result.state == DemoState.FAILED_RECOVERABLE.value
    assert MessageKind.CONFIRMATION.value not in doubles["whatsapp"].kinds()
    assert await deps.slots.active_hold_for(conversation_ref, now=deps.clock.now()) is None


async def test_an_online_demo_without_a_meet_link_is_not_confirmed(
    orchestrator: DemoCommandCenterOrchestrator,
    deps: Dependencies,
    doubles: dict[str, Any],
    seeded_request: Any,
    conversation_ref: str,
) -> None:
    """Telling a parent a demo is online with no link is worse than failing."""
    await advance_to_selection(orchestrator, conversation_ref)
    slot = TimeSlot(starts_at=deps.clock.now() + timedelta(days=1, hours=8))
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.SLOT_AGREED,
        actor=Actor.USER,
        event_id="evt_slot",
        payload={"slot": slot},
    )
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.HOLD_PLACED,
        actor=Actor.SYSTEM,
        event_id="evt_hold",
    )

    doubles["calendar"].omit_conference = True
    result = await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.TUTOR_ACCEPTED,
        actor=Actor.TUTOR,
        event_id="evt_accept",
    )
    assert result.state == DemoState.FAILED_RECOVERABLE.value
    assert doubles["calendar"].events == {}, "the orphan event must be cancelled"


async def test_a_tutor_agent_outage_does_not_strand_the_conversation(
    orchestrator: DemoCommandCenterOrchestrator,
    doubles: dict[str, Any],
    seeded_request: Any,
    conversation_ref: str,
) -> None:
    from demo_command_center.integrations.tutor_intelligence.fake import FakeTutorIntelligence

    doubles["tutors"].empty = True
    assert isinstance(doubles["tutors"], FakeTutorIntelligence)

    for index, (trigger, actor) in enumerate(
        [
            (Trigger.HANDOFF_RECEIVED, Actor.AGENT),
            (Trigger.OWNERSHIP_ACQUIRED, Actor.SYSTEM),
            (Trigger.IDENTITY_RESOLVED, Actor.SYSTEM),
            (Trigger.REQUIREMENTS_COMPLETE, Actor.SYSTEM),
        ]
    ):
        await orchestrator.handle(
            conversation_ref=conversation_ref,
            trigger=trigger,
            actor=actor,
            event_id=f"evt_{index}",
        )

    result = await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.MATCH_EMPTY,
        actor=Actor.SYSTEM,
        event_id="evt_empty",
    )
    assert result.state == DemoState.HUMAN_HANDOFF.value


# ==================================================== the outbound boundary


async def test_a_message_is_suppressed_once_a_human_owns_the_conversation(
    orchestrator: DemoCommandCenterOrchestrator, deps: Dependencies, conversation_ref: str
) -> None:
    """A worker that finishes late must not talk over the human who took over."""
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.HANDOFF_RECEIVED,
        actor=Actor.AGENT,
        event_id="evt_0",
    )
    await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=Trigger.HUMAN_REQUESTED,
        actor=Actor.USER,
        event_id="evt_human",
    )

    late = OutboundMessage(
        conversation_ref=conversation_ref,
        recipient_ref=conversation_ref,
        audience=Party.STUDENT,
        kind=MessageKind.FOLLOWUP,
        body="A late worker still had something to say.",
        idempotency_key=idempotency_key("late", conversation_ref),
        created_at=deps.clock.now(),
    )
    result = await deps.outbound.send(late)
    assert result.outcome is SendOutcome.SUPPRESSED_STATE


async def test_the_same_message_is_never_sent_twice(
    deps: Dependencies, doubles: dict[str, Any], conversation_ref: str
) -> None:
    await deps.conversations.touch_inbound(conversation_ref, at=deps.clock.now())
    await _own(deps, conversation_ref)

    message = OutboundMessage(
        conversation_ref=conversation_ref,
        recipient_ref=conversation_ref,
        audience=Party.STUDENT,
        kind=MessageKind.QUESTION,
        body="Which class is the student in?",
        idempotency_key=idempotency_key("dup", conversation_ref),
        created_at=deps.clock.now(),
    )
    first = await deps.outbound.send(message)
    second = await deps.outbound.send(message)

    assert first.delivered
    assert second.outcome is SendOutcome.DUPLICATE
    assert len(doubles["whatsapp"].sent) == 1


async def test_an_opted_out_recipient_receives_no_marketing_but_still_gets_a_receipt(
    deps: Dependencies, doubles: dict[str, Any], conversation_ref: str
) -> None:
    await deps.conversations.touch_inbound(conversation_ref, at=deps.clock.now())
    await _own(deps, conversation_ref)
    doubles["contacts"].opted_out_refs.add(conversation_ref)

    def message(kind: MessageKind, key: str) -> OutboundMessage:
        return OutboundMessage(
            conversation_ref=conversation_ref,
            recipient_ref=conversation_ref,
            audience=Party.STUDENT,
            kind=kind,
            body="Your demo class is confirmed.",
            idempotency_key=idempotency_key(key, conversation_ref),
            created_at=deps.clock.now(),
        )

    followup = await deps.outbound.send(message(MessageKind.FOLLOWUP, "f"))
    confirmation = await deps.outbound.send(message(MessageKind.CONFIRMATION, "c"))

    assert followup.outcome is SendOutcome.SUPPRESSED_OPT_OUT
    assert confirmation.delivered, "a transactional confirmation is not marketing"


async def test_a_free_form_message_outside_the_session_window_is_refused(
    deps: Dependencies, conversation_ref: str
) -> None:
    """Meta drops it silently; we refuse it loudly and record the outcome."""
    await _own(deps, conversation_ref)
    await deps.conversations.touch_inbound(
        conversation_ref, at=deps.clock.now() - timedelta(hours=48)
    )
    message = OutboundMessage(
        conversation_ref=conversation_ref,
        recipient_ref=conversation_ref,
        audience=Party.STUDENT,
        kind=MessageKind.FOLLOWUP,
        body="Just checking in.",
        idempotency_key=idempotency_key("stale-window", conversation_ref),
        created_at=deps.clock.now(),
    )
    result = await deps.outbound.send(message)
    assert result.outcome is SendOutcome.SUPPRESSED_NO_TEMPLATE


async def test_an_expired_reminder_is_dropped_rather_than_sent_late(
    deps: Dependencies, conversation_ref: str
) -> None:
    await _own(deps, conversation_ref)
    await deps.conversations.touch_inbound(conversation_ref, at=deps.clock.now())
    message = OutboundMessage(
        conversation_ref=conversation_ref,
        recipient_ref=conversation_ref,
        audience=Party.STUDENT,
        kind=MessageKind.SLOT_PROPOSAL,
        body="These times work.",
        idempotency_key=idempotency_key("expired", conversation_ref),
        expires_at=deps.clock.now() - timedelta(minutes=1),
        created_at=deps.clock.now() - timedelta(hours=1),
    )
    result = await deps.outbound.send(message)
    assert result.outcome is SendOutcome.SUPPRESSED_EXPIRED


async def test_a_message_containing_an_unapproved_link_is_blocked(
    deps: Dependencies, conversation_ref: str
) -> None:
    await _own(deps, conversation_ref)
    await deps.conversations.touch_inbound(conversation_ref, at=deps.clock.now())
    message = OutboundMessage(
        conversation_ref=conversation_ref,
        recipient_ref=conversation_ref,
        audience=Party.STUDENT,
        kind=MessageKind.FOLLOWUP,
        body="Pay here: https://evil.test/collect",
        idempotency_key=idempotency_key("badlink", conversation_ref),
        created_at=deps.clock.now(),
    )
    result = await deps.outbound.send(message)
    assert result.outcome is SendOutcome.SUPPRESSED_GUARDRAIL
    assert "unapproved_url" in result.detail


# ------------------------------------------------------------------ helpers


async def _own(deps: Dependencies, conversation_ref: str) -> None:
    ownership = await deps.conversations.load_ownership(conversation_ref, now=deps.clock.now())
    await deps.conversations.save_ownership(ownership.acquire(now=deps.clock.now()))


async def test_a_no_show_needs_evidence_the_calendar_actually_observed(
    orchestrator: DemoCommandCenterOrchestrator,
    deps: Dependencies,
    doubles: dict[str, Any],
    seeded_request: Any,
    conversation_ref: str,
) -> None:
    """Nobody joined. That is a no-show; a model's opinion would not be."""
    demo = Demo(
        demo_id="dmo_noshow",
        conversation_ref=conversation_ref,
        request_id=seeded_request.request_id,
        tutor_ref="tut_fixture_anaya",
        mode=DemoMode.ONLINE,
        slot=TimeSlot(starts_at=deps.clock.now() - timedelta(hours=2)),
        calendar_event_id="evt_noshow",
        meet_url="https://meet.google.com/abc-defg-hij",
        attendees=(DemoAttendee(party=Party.STUDENT, ref=conversation_ref),),
        created_at=deps.clock.now(),
        updated_at=deps.clock.now(),
    )
    await deps.demos.save(demo)
    doubles["calendar"].events["evt_noshow"] = {
        "event_id": "evt_noshow",
        "attendees": [],
        "participant_count": 0,
        "duration_minutes": 0,
    }

    from demo_command_center.orchestration.composer import attendance_from_calendar

    outcome = attendance_from_calendar(doubles["calendar"].events["evt_noshow"])
    assert outcome.student_attended is False
    assert outcome.tutor_attended is False
    assert outcome.authoritative, "a zero-participant conference is authoritative"


def test_a_single_joiner_asserts_nothing_about_who_it_was() -> None:
    """Which party joined is unknowable from a count. Guessing marks the wrong
    person a no-show, which is a complaint from a tutor or a parent."""
    from demo_command_center.orchestration.composer import attendance_from_calendar

    outcome = attendance_from_calendar(
        {"event_id": "e", "attendees": [], "participant_count": 1, "duration_minutes": 5}
    )
    assert outcome.student_attended is None
    assert outcome.tutor_attended is None

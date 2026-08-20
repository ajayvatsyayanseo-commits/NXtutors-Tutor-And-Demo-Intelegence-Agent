"""The full demo lifecycle, through the real orchestrator.

Every assertion here is about the system as it actually runs: the state machine,
the ownership checks, the guards, the idempotency claims and the single outbound
boundary are all live. Only the provider edges are doubled.
"""

from __future__ import annotations

from typing import Any

import pytest

from demo_command_center.contracts.ownership import Owner
from demo_command_center.domain.messages import MessageKind
from demo_command_center.glue.lifecycle import LifecycleRunner
from demo_command_center.orchestration.context import Dependencies
from demo_command_center.orchestration.orchestrator import DemoCommandCenterOrchestrator
from demo_command_center.state.states import DemoState

pytestmark = pytest.mark.e2e


@pytest.fixture
async def completed(
    orchestrator: DemoCommandCenterOrchestrator, deps: Dependencies, doubles: dict[str, Any]
):  # type: ignore[no-untyped-def]
    runner = LifecycleRunner(orchestrator, deps, doubles, conversation_ref="cv_e2e")
    return await runner.run(), deps, doubles


async def test_the_whole_lifecycle_reaches_converted(completed) -> None:  # type: ignore[no-untyped-def]
    report, _, _ = completed
    failures = [f"{s.index}. {s.name}: {s.detail}" for s in report.steps if not s.ok]
    assert failures == [], "\n".join(failures)
    assert report.final_state == DemoState.CONVERTED.value


async def test_tutor_intelligence_was_asked_in_return_only_mode(completed) -> None:  # type: ignore[no-untyped-def]
    """The double-send guard, verified against what was actually sent."""
    _, _, doubles = completed
    calls = doubles["tutors"].calls
    assert calls, "the tutor agent was never consulted"
    assert all(call.return_only for call in calls)


async def test_every_candidate_presented_came_from_the_tutor_agent(completed) -> None:  # type: ignore[no-untyped-def]
    """No stored candidate was invented locally.

    The fake's fixture refs are the only ones it can return, so containment
    against that set is the check. Deliberately does not re-invoke the fake —
    doing so appends to the very `calls` list under inspection.
    """
    from demo_command_center.integrations.tutor_intelligence.fake import _FIXTURES

    _, deps, doubles = completed
    stored = await deps.demos.load_candidates("cv_e2e")
    assert stored, "no candidates were ever presented"
    assert {c.tutor_ref for c in stored} <= {row[0] for row in _FIXTURES}


async def test_exactly_one_agent_sent_each_message(completed) -> None:  # type: ignore[no-untyped-def]
    """No duplicate replies: every idempotency key appears at most once."""
    _, _, doubles = completed
    keys = [message.idempotency_key for message, _ in doubles["whatsapp"].sent]
    assert len(keys) == len(set(keys))


async def test_the_customer_journey_messages_were_all_delivered(completed) -> None:  # type: ignore[no-untyped-def]
    _, _, doubles = completed
    kinds = set(doubles["whatsapp"].kinds())
    assert MessageKind.TUTOR_OPTIONS.value in kinds
    assert MessageKind.SLOT_PROPOSAL.value in kinds
    assert MessageKind.CONFIRMATION.value in kinds
    assert MessageKind.PAYMENT_LINK.value in kinds
    assert MessageKind.WELCOME.value in kinds


async def test_the_confirmation_carries_a_real_google_meet_link(completed) -> None:  # type: ignore[no-untyped-def]
    """A Meet URL that did not come from Google is refused by the guardrail."""
    _, deps, _ = completed
    demo = await deps.demos.for_conversation("cv_e2e")
    assert demo is not None
    assert demo.meet_url and demo.meet_url.startswith("https://meet.google.com/")
    assert demo.calendar_event_id


async def test_only_one_logical_calendar_event_exists_after_a_reschedule(completed) -> None:  # type: ignore[no-untyped-def]
    """A parent must not collect a second invite for one demo."""
    _, deps, doubles = completed
    demo = await deps.demos.for_conversation("cv_e2e")
    assert demo is not None
    assert demo.revision > 1, "the lifecycle must exercise a reschedule"
    assert len(doubles["calendar"].events) == 1


async def test_ownership_moved_to_onboarding_only_after_the_welcome(completed) -> None:  # type: ignore[no-untyped-def]
    _, deps, doubles = completed
    ownership = await deps.conversations.load_ownership("cv_e2e", now=deps.clock.now())
    assert ownership.owner is Owner.ONBOARDING
    assert MessageKind.WELCOME.value in doubles["whatsapp"].kinds()


async def test_the_outbox_published_the_public_lifecycle_events(completed) -> None:  # type: ignore[no-untyped-def]
    _, _, doubles = completed
    events = set(doubles["stores"]["outbox"].events())
    assert {
        "demo.slot_held",
        "demo.scheduled",
        "demo.payment_requested",
        "subscription.activated",
        "onboarding.requested",
    } <= events


async def test_the_transition_history_is_complete_and_versioned(completed) -> None:  # type: ignore[no-untyped-def]
    """Every move is auditable, and the version increments monotonically."""
    _, deps, _ = completed
    history = await deps.conversations.history("cv_e2e", limit=200)
    assert len(history) >= 20
    versions = [row["version"] for row in history]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))


async def test_the_objection_analysis_and_forecast_were_persisted(completed) -> None:  # type: ignore[no-untyped-def]
    _, deps, _ = completed
    demo = await deps.demos.for_conversation("cv_e2e")
    assert demo is not None
    analysis = await deps.analysis.load_objections(demo.demo_id)
    forecast = await deps.analysis.load_forecast(demo.demo_id)
    assert analysis is not None
    assert forecast is not None
    assert 0.0 <= forecast["probability"] <= 1.0
    assert forecast["policy_stamp"].startswith("forecast@v1#")


async def test_the_payment_amount_matches_the_authorised_offer(completed) -> None:  # type: ignore[no-untyped-def]
    _, deps, _ = completed
    order = await deps.commerce.order_for_conversation("cv_e2e")
    demo = await deps.demos.for_conversation("cv_e2e")
    assert order is not None and demo is not None
    decision = await deps.commerce.load_decision(demo.demo_id)
    assert decision is not None
    assert order.amount_minor == decision.payable_minor
    assert order.offer_policy_stamp == decision.policy_stamp


async def test_the_subscription_was_activated_exactly_once(completed) -> None:  # type: ignore[no-untyped-def]
    _, deps, doubles = completed
    order = await deps.commerce.order_for_conversation("cv_e2e")
    assert order is not None
    activation = await deps.commerce.activation_for(order.order_ref)
    assert activation is not None and activation.succeeded
    assert len(doubles["gateway"].activation_calls) == 1


async def test_no_message_body_leaks_an_internal_reference(completed) -> None:  # type: ignore[no-untyped-def]
    """The output guardrail ran on every delivered message."""
    _, _, doubles = completed
    for body in doubles["whatsapp"].bodies():
        for token in ("conversation_ref", "tutor_ref", "policy_stamp", "cv_", "dmo_", "None"):
            assert token not in body, f"{token!r} leaked into: {body[:80]}"


async def test_a_redelivered_event_changes_nothing(
    orchestrator: DemoCommandCenterOrchestrator, deps: Dependencies, doubles: dict[str, Any]
) -> None:
    """Meta redelivers webhooks. A duplicate must be a no-op, not a second turn."""
    from demo_command_center.state.triggers import Actor, Trigger

    first = await orchestrator.handle(
        conversation_ref="cv_dup",
        trigger=Trigger.HANDOFF_RECEIVED,
        actor=Actor.AGENT,
        event_id="evt_same",
    )
    second = await orchestrator.handle(
        conversation_ref="cv_dup",
        trigger=Trigger.HANDOFF_RECEIVED,
        actor=Actor.AGENT,
        event_id="evt_same",
    )
    assert first.accepted and not first.duplicate
    assert second.duplicate
    history = await deps.conversations.history("cv_dup")
    assert len(history) == 1, "a redelivery must not produce a second transition"

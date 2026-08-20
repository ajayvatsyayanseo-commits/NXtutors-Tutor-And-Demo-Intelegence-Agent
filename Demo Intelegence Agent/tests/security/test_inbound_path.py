"""The path from a parent's WhatsApp message to a state transition.

This whole path was missing and nothing failed. `_enqueue_meta` flattened
Meta's batch, claimed an idempotency key for each item, and returned the count
— it never published anything, no queue publisher existed, and no code turned a
sentence into a trigger. A real message was received, signature-verified,
deduplicated and dropped. The webhook answered 200, every metric looked
healthy, and the orchestrator never ran a single turn.

Filed under `security` because the router's output moves money, bookings and a
tutor's evening, and because "answers 200 and does nothing" is the failure mode
no alarm catches.
"""

from __future__ import annotations

from typing import Any

import pytest

from demo_command_center.orchestration.inbound import capture, clarification, route
from demo_command_center.state.machine import StateMachine, StateSnapshot
from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import Actor, Trigger

pytestmark = pytest.mark.security

MACHINE = StateMachine()


def at(state: DemoState) -> StateSnapshot:
    return StateSnapshot(conversation_ref="cv_router", state=state)


def routed(text: str, state: DemoState, button: str | None = None) -> Any:
    return route(text=text, button_id=button, snapshot=at(state), machine=MACHINE)


class TestTheRouterNeverExceedsTheStateMachine:
    """The property everything else rests on: it is gated on the real table."""

    @pytest.mark.parametrize("state", list(DemoState))
    @pytest.mark.parametrize(
        "text",
        ["1", "yes", "cancel", "reschedule", "human", "show me others", "maths class 10", "hello"],
    )
    def test_any_message_in_any_state_yields_only_a_permitted_trigger(
        self, state: DemoState, text: str
    ) -> None:
        result = routed(text, state)
        if result.trigger is None:
            return
        assert result.trigger in MACHINE.available(at(state), actor=Actor.USER), (
            f"router returned {result.trigger.value}, illegal for a user in {state.value}"
        )

    @pytest.mark.parametrize("state", list(DemoState))
    def test_a_button_never_yields_an_impermissible_trigger(self, state: DemoState) -> None:
        for button in ("tutor:1", "slot:2", "tutor:9", "nonsense"):
            result = route(text="", button_id=button, snapshot=at(state), machine=MACHINE)
            if result.trigger is not None:
                assert result.trigger in MACHINE.available(at(state), actor=Actor.USER)


class TestEscapeHatches:
    def test_stop_cancels_even_mid_selection(self) -> None:
        assert (
            routed("stop", DemoState.AWAITING_TUTOR_SELECTION).trigger is Trigger.CANCELLED_BY_USER
        )

    def test_stop_beats_an_ordinal_in_the_same_message(self) -> None:
        """ "cancel option 1" is a cancellation, not a selection."""
        assert (
            routed("cancel option 1", DemoState.AWAITING_TUTOR_SELECTION).trigger
            is Trigger.CANCELLED_BY_USER
        )

    def test_a_human_request_outranks_a_cancellation(self) -> None:
        """Someone angry enough to say both should reach a person."""
        assert (
            routed("cancel this, get me a human", DemoState.SCHEDULED).trigger
            is Trigger.HUMAN_REQUESTED
        )

    def test_someone_else_in_front_of_a_shortlist_means_another_tutor(self) -> None:
        assert (
            routed("someone else please", DemoState.AWAITING_TUTOR_SELECTION).trigger
            is Trigger.OPTIONS_REJECTED
        )

    def test_but_speaking_to_someone_else_still_reaches_a_human(self) -> None:
        assert (
            routed("can I speak to someone else", DemoState.SCHEDULED).trigger
            is Trigger.HUMAN_REQUESTED
        )


class TestSelection:
    def test_an_ordinal_picks_a_tutor_while_awaiting_selection(self) -> None:
        result = routed("2", DemoState.AWAITING_TUTOR_SELECTION)
        assert result.trigger is Trigger.TUTOR_CHOSEN
        assert result.payload["ordinal"] == 2

    def test_the_same_ordinal_picks_a_slot_while_negotiating(self) -> None:
        """The word does not decide which list it means — the state does."""
        result = routed("2", DemoState.NEGOTIATING_SLOT)
        assert result.trigger is Trigger.SLOT_AGREED
        assert result.payload["ordinal"] == 2

    def test_hinglish_ordinals_work(self) -> None:
        assert routed("dusra", DemoState.AWAITING_TUTOR_SELECTION).payload["ordinal"] == 2

    def test_a_name_is_treated_as_a_selection_attempt(self) -> None:
        """Resolution against the stored snapshot happens downstream."""
        result = routed("book Ajay sir please", DemoState.AWAITING_TUTOR_SELECTION)
        assert result.trigger is Trigger.TUTOR_CHOSEN
        assert "ajay" in result.payload["text"].lower()

    def test_a_stale_button_is_refused_not_guessed(self) -> None:
        result = route(text="", button_id="slot:1", snapshot=at(DemoState.PAYMENT_PENDING))
        assert result.trigger is None
        assert result.reason.startswith("button_not_valid_here")


class TestRefusal:
    def test_an_unmatched_message_returns_no_trigger(self) -> None:
        assert not routed("the weather is nice", DemoState.PAYMENT_PENDING).understood

    def test_an_empty_message_does_nothing(self) -> None:
        assert routed("   ", DemoState.COLLECTING_REQUIREMENTS).reason == "empty_message"

    def test_every_refusal_produces_something_to_say(self) -> None:
        """Silence is the worst outcome: the parent cannot tell whether anyone
        read it, and the conversation stalls with no error to explain why."""
        for state in DemoState:
            result = routed("qwertyuiop", state)
            if not result.understood:
                assert clarification(result, at(state)).strip()


class TestRequirementCapture:
    def test_one_sentence_can_complete_the_whole_requirement(self) -> None:
        from demo_command_center.contracts.common import DemoMode, Requirement

        result = capture("maths tutor class 10 CBSE board exam prep online", Requirement())
        assert result.subject == "Mathematics"
        assert result.board == "CBSE"
        assert result.student_class == "10"
        assert result.service == "board_exam_prep"
        assert result.mode is DemoMode.ONLINE
        assert result.complete

    def test_capture_is_incremental_across_turns(self) -> None:
        from demo_command_center.contracts.common import DemoMode, Requirement

        first = capture("I need help with physics", Requirement())
        second = capture("class 12, ICSE", first)
        third = capture("at home please, JEE prep", second)
        assert (third.subject, third.student_class, third.board) == ("Physics", "12", "ICSE")
        assert third.mode is DemoMode.HOME
        assert third.service == "competitive_exam"

    def test_an_earlier_answer_is_never_overwritten(self) -> None:
        """A later mention must not silently rewrite a confirmed fact."""
        from demo_command_center.contracts.common import Requirement

        first = capture("CBSE maths", Requirement())
        second = capture("my nephew does ICSE chemistry", first)
        assert second.board == "CBSE"
        assert second.subject == "Mathematics"

    def test_nothing_recognisable_leaves_it_untouched(self) -> None:
        """So `missing()` keeps asking instead of proceeding on a guess."""
        from demo_command_center.contracts.common import Requirement

        before = Requirement()
        assert capture("hello, is anyone there?", before) is before

    def test_an_unknown_board_is_left_missing_rather_than_guessed(self) -> None:
        from demo_command_center.contracts.common import Requirement

        result = capture("he studies under the Rajasthan syllabus", Requirement())
        assert result.board is None
        assert "board" in result.missing()


class TestTheWebhookProducesRoutableWork:
    """`_work_item` is what the ingress function puts on the queue."""

    class _Pseudonymiser:
        def phone(self, value: str) -> str:
            return f"ph_{value[-4:]}"

    def _deps(self) -> Any:
        outer = self

        class _Deps:
            pseudonymiser = outer._Pseudonymiser()

        return _Deps()

    def _item(self, message: dict[str, Any]) -> Any:
        from demo_command_center.handlers.webhooks import _work_item

        return _work_item({"kind": "inbound_message", "message": message}, self._deps())

    def test_a_text_message_carries_its_text(self) -> None:
        item = self._item(
            {
                "from": "919999000011",
                "id": "wamid.1",
                "type": "text",
                "text": {"body": "class 11 CBSE physics"},
            }
        )
        assert item["kind"] == "inbound_message"
        assert item["text"] == "class 11 CBSE physics"
        assert item["event_id"] == "wamid.1"

    def test_a_button_reply_carries_its_id(self) -> None:
        item = self._item(
            {
                "from": "919999000011",
                "id": "wamid.2",
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "tutor:1", "title": "1"},
                },
            }
        )
        assert item["button_id"] == "tutor:1"

    def test_the_conversation_ref_is_pseudonymised_never_the_number(self) -> None:
        """Nothing downstream may store a raw phone number."""
        item = self._item(
            {"from": "919999000011", "id": "wamid.3", "type": "text", "text": {"body": "hi"}}
        )
        assert "919999000011" not in item["conversation_ref"]

    def test_an_unroutable_media_message_still_produces_an_item(self) -> None:
        """Passed through with empty text so the worker asks a question rather
        than the message vanishing."""
        item = self._item(
            {"from": "919999000011", "id": "wamid.4", "type": "image", "image": {"id": "media1"}}
        )
        assert item is not None
        assert item["text"] == ""

    def test_a_message_with_no_sender_is_dropped(self) -> None:
        assert self._item({"id": "wamid.5", "type": "text", "text": {"body": "hi"}}) is None


class TestTheQueuePublisher:
    async def test_a_missing_queue_url_is_refused_not_swallowed(self) -> None:
        """Swallowing it is exactly how the inbound path became inert."""
        from demo_command_center.contracts.ports import ProviderUnavailable
        from demo_command_center.storage.queue import SqsPublisher

        with pytest.raises(ProviderUnavailable):
            await SqsPublisher(client=object()).publish(queue_url="", body={"a": 1})

    async def test_a_fifo_queue_gets_a_group_and_dedup_id(self) -> None:
        """Without both, SQS rejects the send and one conversation's turns can
        be processed out of order."""
        from demo_command_center.storage.queue import SqsPublisher

        sent: dict[str, Any] = {}

        class FakeSqs:
            def send_message(self, **kw: Any) -> dict[str, str]:
                sent.update(kw)
                return {"MessageId": "m1"}

        await SqsPublisher(client=FakeSqs()).publish(
            queue_url="https://sqs/q.fifo", body={"x": 1}, group_id="cv_1", dedup_id="d1"
        )
        assert sent["MessageGroupId"] == "cv_1"
        assert sent["MessageDeduplicationId"] == "d1"

    async def test_a_standard_queue_gets_neither(self) -> None:
        from demo_command_center.storage.queue import SqsPublisher

        sent: dict[str, Any] = {}

        class FakeSqs:
            def send_message(self, **kw: Any) -> dict[str, str]:
                sent.update(kw)
                return {"MessageId": "m1"}

        await SqsPublisher(client=FakeSqs()).publish(queue_url="https://sqs/q", body={"x": 1})
        assert "MessageGroupId" not in sent

    def test_a_configured_queue_url_selects_sqs(self) -> None:
        from demo_command_center.config.settings import Settings
        from demo_command_center.storage.queue import NullPublisher, SqsPublisher

        assert isinstance(
            __import__(
                "demo_command_center.storage.queue", fromlist=["build_publisher"]
            ).build_publisher(Settings(work_queue_url="https://sqs/q")),
            SqsPublisher,
        )
        assert isinstance(
            __import__(
                "demo_command_center.storage.queue", fromlist=["build_publisher"]
            ).build_publisher(Settings(work_queue_url="")),
            NullPublisher,
        )

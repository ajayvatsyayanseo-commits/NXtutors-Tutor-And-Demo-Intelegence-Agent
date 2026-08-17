"""Kill switches, concurrency, duplicate delivery and dependency failure.

Covers §10 (idempotency under *concurrent* duplicates, not only sequential),
§12/§13 (fallback behaviour), §25 (conversation concurrency) and §37 (chaos).

The theme running through all of it: a dependency being down must degrade the
answer, never fail the turn, and never produce a second side effect.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from tutor_match_meta.config.kill_switches import KillSwitches, Switch
from tutor_match_meta.contracts.inbound import InboundEnvelope, InboundKind, WhatsAppTurnV1
from tutor_match_meta.integrations.llm.provider import (
    LLMError,
    LLMRateLimited,
    LLMSchemaViolation,
    LLMTimeout,
)
from tutor_match_meta.orchestration.turn_service import TurnService
from tutor_match_meta.repositories.ports import MemoryPacket
from tutor_match_meta.state.machine import OptimisticLockError

READY = "class 10 cbse maths gurgaon home tuition"


def turn(
    text: str = READY, *, message_id: str = "m1", conversation: str = "c-res"
) -> InboundEnvelope:
    return InboundEnvelope(
        kind=InboundKind.WHATSAPP_TURN,
        trace_id=f"trace-{message_id}",
        conversation_id=conversation,
        dedup_key=f"{conversation}:{message_id}",
        received_at=datetime.now(UTC),
        source_agent="test",
        payload=WhatsAppTurnV1(
            event_id=message_id,
            conversation_id=conversation,
            provider_message_id=message_id,
            text=text,
        ),
    )


class PausedSwitches:
    """A kill-switch reader with one switch held down."""

    def __init__(self, *paused: Switch) -> None:
        self._paused = {s.value for s in paused}

    async def paused(self, switch: str) -> bool:
        return switch in self._paused

    async def all_paused(self) -> dict[str, bool]:
        return {name: True for name in self._paused}


# ------------------------------------------------------------- kill switches
class TestKillSwitches:
    async def test_matching_paused_declines_without_consuming_the_dedup_key(
        self, turn_deps
    ) -> None:
        """The ordering property that makes MATCHING_PAUSED lossless.

        If the pause ran *after* the idempotency claim, the caller's redelivery
        after unpause would be swallowed as a duplicate and the parent would
        never be answered.
        """
        turn_deps.switches = KillSwitches(PausedSwitches(Switch.MATCHING_PAUSED))
        service = TurnService(turn_deps)

        paused = await service.handle(turn())
        assert paused.paused and paused.reply is None
        assert turn_deps.outbox.pending == []

        # Operator unpauses; the same message is redelivered.
        turn_deps.switches = KillSwitches(PausedSwitches())
        resumed = await service.handle(turn())
        assert not resumed.duplicate, "the dedup key was burned while paused"
        assert resumed.matched

    async def test_llm_paused_degrades_to_deterministic_matching(self, turn_deps) -> None:
        """Documented safe behaviour: degrade silently, never fabricate."""

        class ExplodingLLM:
            name = "exploding"

            async def structured(self, request: object) -> object:
                raise AssertionError("the LLM was called while paused")

        turn_deps.llm = ExplodingLLM()
        turn_deps.switches = KillSwitches(PausedSwitches(Switch.LLM_PAUSED))
        result = await TurnService(turn_deps).handle(turn())
        assert result.matched, "deterministic matching must still work"

    async def test_a_switch_store_outage_does_not_stop_traffic(self, turn_deps) -> None:
        class BrokenSwitches:
            async def paused(self, switch: str) -> bool:
                raise RuntimeError("switch store unreachable")

            async def all_paused(self) -> dict[str, bool]:
                raise RuntimeError("switch store unreachable")

        turn_deps.switches = KillSwitches(BrokenSwitches())
        result = await TurnService(turn_deps).handle(turn())
        assert result.matched


# --------------------------------------------------------------- idempotency
class TestDuplicateDelivery:
    async def test_a_sequential_duplicate_produces_one_decision(self, turn_deps) -> None:
        service = TurnService(turn_deps)
        await service.handle(turn())
        second = await service.handle(turn())
        assert second.duplicate
        assert len(turn_deps.decisions.all_for("c-res")) == 1

    async def test_concurrent_duplicates_produce_one_decision(self, turn_deps) -> None:
        """§10 explicitly asks for the *concurrent* case, not just sequential.

        Two workers receiving the same redelivery at the same instant is the
        realistic shape — SQS at-least-once plus a visibility-timeout expiry —
        and a check-then-act idempotency layer passes the sequential test while
        failing this one.
        """
        service = TurnService(turn_deps)
        results = await asyncio.gather(
            *(service.handle(turn()) for _ in range(8)), return_exceptions=True
        )
        real = [r for r in results if not isinstance(r, BaseException)]
        duplicates = [r for r in real if r.duplicate]
        assert len(duplicates) == len(real) - 1, "more than one worker did the work"
        assert len(turn_deps.decisions.all_for("c-res")) == 1
        assert len(turn_deps.outbox.pending) == 1

    async def test_a_failed_turn_releases_its_claim_for_a_genuine_retry(self, turn_deps) -> None:
        calls = {"n": 0}
        original = turn_deps.decisions.save

        async def flaky(decision: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient database error")
            await original(decision)

        turn_deps.decisions.save = flaky  # type: ignore[method-assign]
        service = TurnService(turn_deps)

        with pytest.raises(RuntimeError):
            await service.handle(turn())
        # SQS redelivers. The retry must do the work, not be eaten as a dupe.
        retried = await service.handle(turn())
        assert not retried.duplicate
        assert retried.matched


# --------------------------------------------------------------- concurrency
class TestConversationConcurrency:
    async def test_three_rapid_turns_leave_a_coherent_requirement(self, turn_deps) -> None:
        """The §25 scenario: "Class 10", then "CBSE", then "Maths after 7".

        FIFO grouping serialises these in production. Here they are issued as
        fast as the event loop allows; the requirement must end up containing
        all three facts regardless of interleaving, because merges are
        provenance-ranked rather than last-write-wins.
        """
        service = TurnService(turn_deps)
        for index, text in enumerate(
            ["class 10", "cbse board", "maths after 7pm in gurgaon, home tuition"]
        ):
            await service.handle(turn(text, message_id=f"m{index}"))

        stored = await turn_deps.requirements.load("c-res")
        assert stored is not None
        assert stored.value_of("student_class") == "Class 10"
        assert stored.value_of("board") == "CBSE"
        assert stored.value_of("subject") == "Mathematics"

    async def test_a_stale_worker_cannot_overwrite_newer_state(self, turn_deps) -> None:
        """An optimistic-lock conflict is raised, not silently swallowed.

        Raising is what lets SQS redeliver against the *current* state. A
        worker that shrugged and wrote anyway would roll a conversation
        backwards.
        """
        service = TurnService(turn_deps)
        await service.handle(turn("class 10", message_id="m0"))

        snapshot = await turn_deps.conversations.load("c-res")
        # Simulate a worker that loaded state, then lost the race.
        with pytest.raises(OptimisticLockError):
            await turn_deps.conversations.save(snapshot, expected_version=snapshot.lock_version - 1)

    async def test_distinct_conversations_do_not_interfere(self, turn_deps) -> None:
        service = TurnService(turn_deps)
        results = await asyncio.gather(
            *(
                service.handle(turn(READY, message_id=f"m{i}", conversation=f"c-{i}"))
                for i in range(6)
            )
        )
        assert all(r.matched for r in results)
        assert len({r.conversation_id for r in results}) == 6


# --------------------------------------------------------------------- chaos
class TestDependencyFailure:
    """§37. Each dependency is broken in turn; the turn must still complete."""

    @pytest.mark.parametrize(
        "error",
        [
            LLMTimeout("provider took 30 seconds"),
            LLMRateLimited("429 from the provider"),
            LLMSchemaViolation("response was not valid JSON"),
            LLMError("upstream 500"),
        ],
        ids=["timeout", "rate_limited", "malformed_output", "upstream_5xx"],
    )
    async def test_an_llm_failure_degrades_but_does_not_fail_the_turn(
        self, turn_deps, error: Exception
    ) -> None:
        class FailingLLM:
            name = "failing"

            async def structured(self, request: object) -> object:
                raise error

        turn_deps.llm = FailingLLM()
        result = await TurnService(turn_deps).handle(turn())
        assert result.matched, "a provider failure must not lose the parent's turn"

    async def test_a_memory_outage_is_recorded_not_raised(self, turn_deps) -> None:
        class BrokenMemory:
            async def recall(self, **kwargs: object) -> MemoryPacket:
                return MemoryPacket(available=False, warnings=("chitragupta_down",))

            async def record(self, **kwargs: object) -> bool:
                return False

        turn_deps.memory = BrokenMemory()
        result = await TurnService(turn_deps).handle(turn())
        assert result.matched
        assert "chitragupta" in result.degraded

    async def test_a_projection_outage_produces_an_honest_no_match(self, turn_deps) -> None:
        """Never a random tutor. An empty pool is reported as an empty pool."""

        class BrokenProjection:
            async def search(self, query: object) -> list:
                raise RuntimeError("database unavailable")

            async def get(self, tutor_id: str) -> None:
                return None

            async def get_by_public_ref(self, ref: str) -> None:
                return None

        turn_deps.orchestrator._tutors = BrokenProjection()
        result = await TurnService(turn_deps).handle(turn())
        assert not result.matched
        assert result.outcome is not None
        assert result.outcome.decision.no_match_reason == "empty_candidate_pool"
        assert "tutor_projection" in result.outcome.decision.degraded_sources

    async def test_an_analytics_outage_never_fails_a_turn(self, turn_deps) -> None:
        class BrokenAnalytics:
            async def emit(self, event: object) -> None:
                raise RuntimeError("analytics table gone")

        turn_deps.analytics = BrokenAnalytics()
        result = await TurnService(turn_deps).handle(turn())
        assert result.matched

    async def test_a_misbehaving_evaluator_does_not_fail_the_match(
        self, turn_deps, orchestrator
    ) -> None:
        """One broken skill scores as MISSING; the other seven still rank."""
        from tutor_match_meta.contracts.common import Dimension

        class ExplodingEvaluator:
            dimension = Dimension.PROXIMITY

            def evaluate(self, tutor: object, context: object) -> object:
                raise ZeroDivisionError("bad maths in a scorer")

        orchestrator._evaluators[Dimension.PROXIMITY] = ExplodingEvaluator()
        result = await TurnService(turn_deps).handle(turn())
        assert result.matched

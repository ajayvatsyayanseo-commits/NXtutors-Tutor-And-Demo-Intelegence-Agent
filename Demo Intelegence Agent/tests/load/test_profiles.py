"""Load profiles against simulated providers. Nothing real is called or billed.

These are not a substitute for a load test against a deployed stack — they
cannot measure Lambda cold starts or SQS behaviour. What they *do* measure is
the thing a deployed test cannot isolate: how much work the orchestrator itself
does per conversation, and whether contention on the slot-hold exclusion
degrades or collapses.

The per-demo counters are the useful output. "OpenAI calls per demo" and
"provider calls per demo" are the two numbers that decide the unit cost, and a
regression in either shows up here long before it shows up on a bill.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from demo_command_center.contracts.common import DemoMode
from demo_command_center.domain.slots import SlotConflict, TimeSlot, new_hold
from demo_command_center.glue.lifecycle import LifecycleRunner
from demo_command_center.orchestration.context import Dependencies
from demo_command_center.orchestration.orchestrator import DemoCommandCenterOrchestrator
from demo_command_center.state.triggers import Actor, Trigger

pytestmark = pytest.mark.load


@dataclass(frozen=True, slots=True)
class Profile:
    """A load shape. Sized to run in CI; the ratios are what matter."""

    name: str
    conversations: int
    concurrency: int
    #: Wall-clock ceiling. Generous — this asserts "does not collapse", not a
    #: latency SLO, which only a deployed test can measure honestly.
    budget_seconds: float


PROFILES: dict[str, Profile] = {
    "baseline": Profile("baseline", conversations=10, concurrency=2, budget_seconds=30),
    "peak": Profile("peak", conversations=30, concurrency=10, budget_seconds=60),
    "burst": Profile("burst", conversations=40, concurrency=40, budget_seconds=60),
    "stress": Profile("stress", conversations=60, concurrency=60, budget_seconds=90),
    "soak": Profile("soak", conversations=80, concurrency=5, budget_seconds=120),
}


@dataclass(slots=True)
class Measurements:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    completed: int = 0

    def percentile(self, p: float) -> float:
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        index = min(int(len(ordered) * p), len(ordered) - 1)
        return round(ordered[index], 2)

    def report(self, profile: Profile) -> str:
        return (
            f"{profile.name}: {self.completed}/{profile.conversations} completed, "
            f"p50={self.percentile(0.5)}ms p95={self.percentile(0.95)}ms "
            f"p99={self.percentile(0.99)}ms errors={self.errors}"
        )


async def _run_profile(
    profile: Profile,
    stack_factory: Any,
) -> Measurements:
    """Drive `conversations` full lifecycles at `concurrency`."""
    measurements = Measurements()
    semaphore = asyncio.Semaphore(profile.concurrency)

    async def one(index: int) -> None:
        async with semaphore:
            orchestrator, deps, doubles = stack_factory()
            started = time.perf_counter()
            try:
                report = await LifecycleRunner(
                    orchestrator, deps, doubles, conversation_ref=f"cv_load_{index}"
                ).run()
                if report.ok:
                    measurements.completed += 1
                else:
                    measurements.errors += 1
            except Exception:
                measurements.errors += 1
            finally:
                measurements.latencies_ms.append((time.perf_counter() - started) * 1000)

    await asyncio.gather(*(one(i) for i in range(profile.conversations)))
    return measurements


@pytest.fixture
def stack_factory(settings, clock):  # type: ignore[no-untyped-def]
    """A fresh stack per conversation — each Lambda invocation is isolated."""
    from demo_command_center.bootstrap import build_local_stack

    def make() -> Any:
        return build_local_stack(settings, clock=clock)

    return make


@pytest.mark.parametrize("profile_name", ["baseline"])
async def test_smoke_profile_completes_every_lifecycle(
    profile_name: str, stack_factory: Any
) -> None:
    """The CI profile. Proves the orchestrator does not degrade under light
    concurrency, and produces the per-demo counters."""
    profile = PROFILES[profile_name]
    started = time.perf_counter()
    measurements = await _run_profile(profile, stack_factory)
    elapsed = time.perf_counter() - started

    print(f"\n{measurements.report(profile)}  wall={elapsed:.2f}s")

    assert measurements.errors == 0, f"{measurements.errors} lifecycles failed"
    assert measurements.completed == profile.conversations
    assert elapsed < profile.budget_seconds


async def test_per_demo_provider_and_llm_counters(stack_factory: Any) -> None:
    """The unit-cost numbers. A regression here is a regression on the bill."""
    orchestrator, deps, doubles = stack_factory()
    await LifecycleRunner(orchestrator, deps, doubles, conversation_ref="cv_cost").run()

    llm_calls = len(getattr(doubles["llm"], "calls", []))
    tutor_calls = len(doubles["tutors"].calls)
    calendar_events = len(doubles["calendar"].events)
    messages = len(doubles["whatsapp"].sent)

    print(
        f"\nper completed demo: llm={llm_calls} tutor_match={tutor_calls} "
        f"calendar_events={calendar_events} messages={messages}"
    )

    # Ceilings, not targets. Crossing one means a loop or a duplicate call.
    assert llm_calls <= 4, "objection extraction should run once per demo"
    assert tutor_calls <= 3, "one match, plus at most a re-match after a decline"
    assert calendar_events == 1, "a reschedule must patch, never create a second event"
    assert messages <= 10


async def test_slot_contention_degrades_rather_than_collapses(
    deps: Dependencies, clock: Any
) -> None:
    """Fifty coroutines, one slot. Exactly one wins; the rest are refused
    cleanly rather than raising something unclassified."""
    slot = TimeSlot(starts_at=clock.now() + timedelta(days=1, hours=8))

    async def attempt(index: int) -> str:
        try:
            await deps.slots.place_hold(
                new_hold(
                    hold_id=f"hld_load_{index}",
                    conversation_ref=f"cv_load_{index}",
                    tutor_ref="tut_contended",
                    slot=slot,
                    mode=DemoMode.ONLINE,
                    now=clock.now(),
                )
            )
        except SlotConflict:
            return "refused"
        return "won"

    started = time.perf_counter()
    outcomes = await asyncio.gather(*(attempt(i) for i in range(50)))
    elapsed_ms = (time.perf_counter() - started) * 1000

    print(f"\n50-way slot contention resolved in {elapsed_ms:.1f}ms")
    assert outcomes.count("won") == 1
    assert outcomes.count("refused") == 49


async def test_no_unbounded_scan_on_the_conversation_path(
    orchestrator: DemoCommandCenterOrchestrator, deps: Dependencies
) -> None:
    """A turn's cost must not grow with the conversation's history.

    The specific regression this guards: loading a full transcript, or every
    transition, to build context. Both are fine at ten turns and fatal at
    a thousand.
    """
    ref = "cv_growth"
    await orchestrator.handle(
        conversation_ref=ref,
        trigger=Trigger.HANDOFF_RECEIVED,
        actor=Actor.AGENT,
        event_id="e0",
    )
    for index in range(60):
        await orchestrator.handle(
            conversation_ref=ref,
            trigger=Trigger.REQUIREMENTS_UPDATED,
            actor=Actor.USER,
            event_id=f"e{index + 1}",
        )

    # The default history read is bounded regardless of how long the
    # conversation has run.
    assert len(await deps.conversations.history(ref)) <= 50

    early = time.perf_counter()
    await orchestrator.handle(
        conversation_ref=ref,
        trigger=Trigger.REQUIREMENTS_UPDATED,
        actor=Actor.USER,
        event_id="e_final",
    )
    elapsed_ms = (time.perf_counter() - early) * 1000
    print(f"\nturn latency after 60 turns: {elapsed_ms:.1f}ms")
    assert elapsed_ms < 500

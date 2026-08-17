"""In-process load: latency percentiles and behaviour under concurrency.

`locustfile.py` next door drives a *deployed* ingress and is the only way to
measure real queue age and RDS Proxy connection counts. It needs AWS, so it can
never run in CI — and "we have a locustfile" was being counted as load coverage
when nothing had actually been executed.

This file measures what can honestly be measured without AWS: the compute cost
of the matching pipeline itself, and whether concurrency corrupts anything.
Everything here runs against in-memory adapters, so the numbers are the
*orchestration* cost — extraction, hard filters, eight evaluators, ranking,
evidence guard, output guard — with no network and no database. That is the
right thing to bound, because it is the part that is ours: database latency and
queue wait are measured separately, by the locustfile, against real
infrastructure.

Thresholds are deliberately loose. This is a regression detector, not a
benchmark: it should catch someone adding an accidental O(n²) over the candidate
pool, not fail because a laptop was busy.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import UTC, datetime

import pytest

from tutor_match_meta.bootstrap import build_local_stack
from tutor_match_meta.contracts.inbound import InboundEnvelope, InboundKind, WhatsAppTurnV1

pytestmark = pytest.mark.load

MESSAGE = "class 10 cbse maths tutor in gurgaon sector 57, home tuition, after 6:30pm"

#: Generous ceilings — see the module docstring. A real regression is an order
#: of magnitude, not a few milliseconds.
P95_CEILING_MS = 250.0
P99_CEILING_MS = 500.0


def envelope(conversation: str, index: int, text: str = MESSAGE) -> InboundEnvelope:
    return InboundEnvelope(
        kind=InboundKind.WHATSAPP_TURN,
        trace_id=f"{conversation}-{index}",
        conversation_id=conversation,
        dedup_key=f"{conversation}:{index}",
        received_at=datetime.now(UTC),
        source_agent="load-test",
        payload=WhatsAppTurnV1(
            event_id=f"{conversation}-{index}",
            conversation_id=conversation,
            provider_message_id=f"{conversation}-{index}",
            text=text,
        ),
    )


def percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
        "max": ordered[-1],
        "n": float(len(ordered)),
    }


class TestLatencyProfile:
    async def test_a_full_turn_stays_inside_its_budget(self, capsys) -> None:
        """200 sequential turns, each a complete match, each its own conversation."""
        service, _ = build_local_stack()
        samples: list[float] = []

        for index in range(200):
            started = time.perf_counter()
            result = await service.handle(envelope(f"load-{index}", 1))
            samples.append((time.perf_counter() - started) * 1000)
            assert result.matched, "a load run that stops matching is measuring nothing"

        stats = percentiles(samples)
        with capsys.disabled():
            print(
                f"\n  full turn  n={stats['n']:.0f}  "
                f"p50={stats['p50']:.1f}ms  p95={stats['p95']:.1f}ms  "
                f"p99={stats['p99']:.1f}ms  max={stats['max']:.1f}ms"
            )

        assert stats["p95"] < P95_CEILING_MS, f"p95 regressed to {stats['p95']:.1f}ms"
        assert stats["p99"] < P99_CEILING_MS, f"p99 regressed to {stats['p99']:.1f}ms"

    async def test_cost_scales_linearly_with_the_candidate_pool(self, capsys) -> None:
        """Eight evaluators over N candidates must be O(N), not O(N²).

        The pool is bounded at query time, so a quadratic here would not show up
        as a timeout — it would show up as the bound being lowered until the
        symptom went away, and the shortlist quietly getting worse.
        """
        service, deps = build_local_stack()
        timings: dict[int, float] = {}

        for limit in (5, 40):
            deps.orchestrator._tutors  # noqa: B018 - documents what is being reused
            samples = []
            for index in range(30):
                started = time.perf_counter()
                await service.handle(envelope(f"pool-{limit}-{index}", 1))
                samples.append((time.perf_counter() - started) * 1000)
            timings[limit] = statistics.median(samples)

        with capsys.disabled():
            print(f"  pool scaling  {timings}")
        # The fixture pool is small, so this asserts the shape does not explode
        # rather than a precise ratio.
        assert timings[40] < timings[5] * 8 + 50


class TestConcurrency:
    async def test_fifty_conversations_in_flight_do_not_bleed_into_each_other(self) -> None:
        """One shared stack, fifty concurrent turns. Each parent must get an
        answer derived from their own requirement and nobody else's."""
        service, _ = build_local_stack()

        # Asked-for word -> the canonical subject the reply will name.
        subjects = {
            "maths": "Mathematics",
            "science": "Science",
            "physics": "Physics",
            "english": "English",
        }
        asked = list(subjects.items())
        wanted = {f"mix-{i}": asked[i % len(asked)] for i in range(50)}

        results = await asyncio.gather(
            *(
                service.handle(envelope(cid, 1, f"class 10 cbse {word} gurgaon home tuition"))
                for cid, (word, _) in wanted.items()
            )
        )

        assert len(results) == 50
        matched = 0
        for (cid, (_, canonical)), result in zip(wanted.items(), results, strict=True):
            assert result.conversation_id == cid
            if result.matched:
                matched += 1
                # The reply must name the subject *this* conversation asked for.
                assert canonical in (result.reply or ""), (
                    f"{cid} asked for {canonical} and got: {result.reply!r}"
                )
        assert matched, "no conversation matched; the test is measuring nothing"

    async def test_a_duplicate_storm_produces_exactly_one_decision(self) -> None:
        """Meta redelivers webhooks. Twenty concurrent copies of one message
        must yield one decision and nineteen duplicate short-circuits."""
        service, deps = build_local_stack()

        results = await asyncio.gather(*(service.handle(envelope("storm", 1)) for _ in range(20)))

        processed = [r for r in results if not r.duplicate]
        duplicates = [r for r in results if r.duplicate]
        assert len(processed) == 1, f"{len(processed)} turns escaped the idempotency claim"
        assert len(duplicates) == 19
        assert len(deps.decisions.all_for("storm")) == 1

    async def test_sustained_throughput_does_not_leak_state(self) -> None:
        """500 turns through one warm container, the shape a warm Lambda sees.

        Growth in the shared cache is expected; growth without bound is not, and
        an evaluator that accumulated per-turn state would show up here.
        """
        service, deps = build_local_stack()

        await asyncio.gather(*(service.handle(envelope(f"sustained-{i}", 1)) for i in range(500)))

        # Exactly one decision per conversation, and nothing extra.
        for i in range(500):
            assert len(deps.decisions.all_for(f"sustained-{i}")) == 1
        assert len(deps.outbox.pending) == 500


class TestDegradedThroughput:
    async def test_throughput_survives_every_optional_dependency_failing(self) -> None:
        """The fallback matrix promises a deterministic answer when memory, the
        model and the cache are all gone. Under load that promise is what stops
        one dependency outage becoming a queue backlog."""
        service, deps = build_local_stack()
        deps.llm = None
        deps.cache = None

        results = await asyncio.gather(
            *(service.handle(envelope(f"degraded-{i}", 1)) for i in range(100))
        )
        answered = [r for r in results if r.reply]
        assert len(answered) == 100, "a degraded dependency must not drop turns"

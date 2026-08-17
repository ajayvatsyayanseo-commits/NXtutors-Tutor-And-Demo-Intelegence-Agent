"""Behavioural proof for the statements that only PostgreSQL can execute.

Two defects found in the pre-release pass were invisible to the rest of the
suite because the rest of the suite never touches PostgreSQL:

1. the token bucket returned `(tokens >= 0) AS allowed` after an UPDATE that
   clamps `tokens` non-negative — so the limiter **allowed every request**;
2. `claim_batch` held `FOR UPDATE SKIP LOCKED` in an autocommit session and
   never wrote the row, so two relays could deliver the same reply.

Both are properties of concurrent SQL. `tests/unit/test_rate_limit_sql.py`
keeps the *shape* from regressing everywhere; this file is the behavioural
evidence, and the release-gate report records it as NOT EXECUTED wherever
`TMM_INTEGRATION_DSN` was unavailable.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tutor_match_meta.cache.postgres_store import (
    PostgresKeyValueStore,
    PostgresKillSwitchStore,
    PostgresRateLimitStore,
)
from tutor_match_meta.contracts.outbound import OUTBOX_KIND_REPLY
from tutor_match_meta.repositories.ports import OutboxMessage
from tutor_match_meta.repositories.postgres import PostgresIdempotencyStore, PostgresOutbox

pytestmark = pytest.mark.integration


class TestRateLimiterRefuses:
    async def test_the_bucket_refuses_once_it_is_empty(self, sessions, schema: str) -> None:
        """The assertion the previous statement could never satisfy."""
        store = PostgresRateLimitStore(sessions, schema=schema)
        allowed = [
            (await store.consume("k1", capacity=3, refill_per_second=0.0)).allowed for _ in range(5)
        ]
        assert allowed == [True, True, True, False, False]

    async def test_a_refusal_reports_a_usable_retry_after(self, sessions, schema: str) -> None:
        """Retry-After is derived from the deficit and the refill rate.

        Drained at `refill=0` so the refusal is not a race against the clock —
        against a remote instance a round trip can exceed one refill period,
        and the bucket would refill as fast as the test drains it. The rate is
        then supplied only on the call being measured.
        """
        store = PostgresRateLimitStore(sessions, schema=schema)
        for _ in range(2):
            await store.consume("k2", capacity=2, refill_per_second=0.0)
        refused = await store.consume("k2", capacity=2, refill_per_second=1.0)
        assert not refused.allowed
        assert 0 < refused.retry_after_seconds <= 2.0

    async def test_tokens_refill_over_wall_clock_time(self, sessions, schema: str) -> None:
        """The one test that is genuinely about elapsed time.

        A slow refill rate and a generous sleep, so network latency can only
        make the refill *more* complete, never less — the assertion cannot flap
        in the direction that matters.
        """
        store = PostgresRateLimitStore(sessions, schema=schema)
        for _ in range(2):
            await store.consume("k3", capacity=2, refill_per_second=0.0)
        assert not (await store.consume("k3", capacity=2, refill_per_second=0.0)).allowed

        await asyncio.sleep(1.0)  # at 2/s that is 2 tokens, and latency adds more
        assert (await store.consume("k3", capacity=2, refill_per_second=2.0)).allowed

    async def test_concurrent_consumers_cannot_both_take_the_last_token(
        self, sessions, schema: str
    ) -> None:
        """The property an in-Python read-modify-write cannot provide.

        Twenty coroutines race for a bucket holding five tokens. Exactly five
        may win, because the refill-check-decrement is one statement under a
        row lock.
        """
        store = PostgresRateLimitStore(sessions, schema=schema)
        results = await asyncio.gather(
            *(store.consume("hot", capacity=5, refill_per_second=0.0) for _ in range(20))
        )
        assert sum(1 for r in results if r.allowed) == 5

    async def test_a_cost_larger_than_the_bucket_is_refused(self, sessions, schema: str) -> None:
        store = PostgresRateLimitStore(sessions, schema=schema)
        state = await store.consume("k4", capacity=1.0, refill_per_second=1.0, cost=5.0)
        assert not state.allowed

    async def test_independent_keys_do_not_share_a_bucket(self, sessions, schema: str) -> None:
        store = PostgresRateLimitStore(sessions, schema=schema)
        assert (await store.consume("a", capacity=1, refill_per_second=0.0)).allowed
        assert (await store.consume("b", capacity=1, refill_per_second=0.0)).allowed
        assert not (await store.consume("a", capacity=1, refill_per_second=0.0)).allowed


class TestOutboxLease:
    def _message(self, key: str) -> OutboxMessage:
        return OutboxMessage(
            kind=OUTBOX_KIND_REPLY,
            conversation_id="c1",
            payload={
                "kind": OUTBOX_KIND_REPLY,
                "recipient": "+919876543210",
                "body": "hello",
                "dedup_key": key,
                "trace_id": "t",
            },
            dedup_key=key,
            trace_id="t",
        )

    async def test_a_claim_is_exclusive_across_concurrent_relays(
        self, sessions, schema: str
    ) -> None:
        """The duplicate-send defect, stated as a test.

        Ten rows, four relays claiming at once. Every row must be claimed by
        exactly one relay — previously all four saw all ten.
        """
        outbox = PostgresOutbox(sessions)
        for index in range(10):
            await outbox.enqueue(self._message(f"d{index}"))

        now = datetime.now(UTC)
        batches = await asyncio.gather(*(outbox.claim_batch(limit=10, now=now) for _ in range(4)))
        claimed = [m.dedup_key for batch in batches for m in batch]
        assert sorted(claimed) == sorted({*claimed}), "a row was claimed twice"
        assert len(claimed) == 10

    async def test_enqueue_is_idempotent_on_the_dedup_key(self, sessions, schema: str) -> None:
        outbox = PostgresOutbox(sessions)
        await outbox.enqueue(self._message("same"))
        await outbox.enqueue(self._message("same"))
        claimed = await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        assert len(claimed) == 1

    async def test_an_abandoned_lease_is_reclaimed(self, sessions, schema: str) -> None:
        """A relay that died mid-batch must not strand the parent's reply."""
        outbox = PostgresOutbox(sessions)
        await outbox.enqueue(self._message("orphan"))
        await outbox.claim_batch(limit=10, now=datetime.now(UTC))

        # Nothing is claimable while the lease is live.
        assert await outbox.claim_batch(limit=10, now=datetime.now(UTC)) == []
        # Once it expires, it comes back.
        assert await outbox.reclaim_stale(older_than=datetime.now(UTC) + timedelta(hours=1)) == 1
        assert len(await outbox.claim_batch(limit=10, now=datetime.now(UTC))) == 1

    async def test_a_failure_returns_the_row_and_counts_the_attempt(
        self, sessions, schema: str
    ) -> None:
        outbox = PostgresOutbox(sessions)
        await outbox.enqueue(self._message("retry"))
        await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        await outbox.mark_failed(
            "retry", error="sqs unreachable", retry_at=datetime.now(UTC) - timedelta(seconds=1)
        )
        again = await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        assert [m.dedup_key for m in again] == ["retry"]
        assert again[0].attempts == 1

    async def test_a_dead_row_is_never_claimed_again(self, sessions, schema: str) -> None:
        outbox = PostgresOutbox(sessions)
        await outbox.enqueue(self._message("dead"))
        await outbox.claim_batch(limit=10, now=datetime.now(UTC))
        await outbox.mark_dead("dead", error="unaddressable")
        assert await outbox.claim_batch(limit=10, now=datetime.now(UTC)) == []


class TestIdempotencyClaim:
    async def test_concurrent_claims_have_exactly_one_winner(self, sessions, schema: str) -> None:
        store = PostgresIdempotencyStore(sessions)
        results = await asyncio.gather(*(store.claim("dupe", ttl_seconds=60) for _ in range(12)))
        assert sum(results) == 1

    async def test_an_expired_claim_can_be_retaken(self, sessions, schema: str) -> None:
        store = PostgresIdempotencyStore(sessions)
        assert await store.claim("k", ttl_seconds=-1)
        assert await store.claim("k", ttl_seconds=60), "an expired claim blocked a real retry"

    async def test_a_release_frees_the_key(self, sessions, schema: str) -> None:
        store = PostgresIdempotencyStore(sessions)
        assert await store.claim("k2", ttl_seconds=60)
        await store.release("k2")
        assert await store.claim("k2", ttl_seconds=60)


class TestSharedStores:
    async def test_a_kv_entry_expires(self, sessions, schema: str) -> None:
        store = PostgresKeyValueStore(sessions, schema=schema)
        await store.set("k", "v", ttl_seconds=-1)
        assert await store.get("k") is None

    async def test_clear_prefix_removes_a_namespace(self, sessions, schema: str) -> None:
        store = PostgresKeyValueStore(sessions, schema=schema)
        await store.set("v1:pool:a", "1", ttl_seconds=60)
        await store.set("v1:pool:b", "2", ttl_seconds=60)
        await store.set("v1:geo:c", "3", ttl_seconds=60)
        assert await store.clear_prefix("v1:pool:") == 2
        assert await store.get("v1:geo:c") == "3"

    async def test_a_kill_switch_is_visible_within_its_ttl(self, sessions, schema: str) -> None:
        store = PostgresKillSwitchStore(sessions, schema=schema, ttl_seconds=0)
        assert not await store.paused("LLM_PAUSED")
        await store.set("LLM_PAUSED", paused=True, actor="oncall", reason="cost spike")
        assert await store.paused("LLM_PAUSED")

    async def test_who_and_why_are_mandatory_and_recorded(self, sessions, schema: str) -> None:
        from sqlalchemy import text

        store = PostgresKillSwitchStore(sessions, schema=schema, ttl_seconds=0)
        await store.set("OUTBOUND_PAUSED", paused=True, actor="ajay", reason="meta outage")
        async with sessions() as session:
            row = (
                await session.execute(
                    text(f"SELECT actor, reason FROM {schema}.kill_switch WHERE name = :n"),
                    {"n": "OUTBOUND_PAUSED"},
                )
            ).one()
        assert row.actor == "ajay" and row.reason == "meta outage"

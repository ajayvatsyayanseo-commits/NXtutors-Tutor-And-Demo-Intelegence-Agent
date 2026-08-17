"""The shared token bucket must actually refuse.

`PostgresRateLimitStore` is the only limiter that matters in production — the
in-process one is `N_containers × limit`, which is not a limit. It is also the
one the test suite could not previously reach, and that gap hid a real defect:
the statement returned `(tokens >= 0) AS allowed`, which was a tautology,
because the UPDATE branch never wrote a negative value. **The limiter allowed
every request it ever saw.**

The behavioural proof needs PostgreSQL and lives in
`tests/integration/test_postgres_stores.py`. What runs everywhere is this: a
structural check that the specific mistake cannot come back, plus a full
behavioural suite against the in-memory store that both implementations must
satisfy.
"""

from __future__ import annotations

import inspect
import re

import pytest

from tutor_match_meta.cache.postgres_store import PostgresRateLimitStore
from tutor_match_meta.security.rate_limit import (
    Enforcement,
    InMemoryBucketStore,
    LayeredRateLimiter,
    LimitPolicy,
    LimitScope,
)


class TestTheRegression:
    """Guards on the exact shape of the statement that was wrong."""

    def _source(self) -> str:
        return inspect.getsource(PostgresRateLimitStore.consume)

    def test_the_tautological_allowed_expression_is_gone(self) -> None:
        """`(tokens >= 0) AS allowed` after a clamped UPDATE is always true."""
        source = self._source()
        assert not re.search(r"tokens\s*>=\s*0\s*\)\s*AS\s+allowed", source, re.IGNORECASE), (
            "the refusal is being derived from a value the UPDATE clamps non-negative"
        )

    def test_the_decrement_is_conditional(self) -> None:
        """Refusal is the *absence* of an updated row, not a returned flag."""
        source = self._source()
        assert re.search(r"WHERE\s+\{self\._REFILLED\}\s*>=", source), (
            "ON CONFLICT DO UPDATE has no WHERE guard, so it can never refuse"
        )
        assert "one_or_none()" in source, "the code must treat zero returned rows as the refusal"

    def test_a_cost_larger_than_the_bucket_is_refused_without_a_query(self) -> None:
        source = self._source()
        assert "if cost > capacity" in source

    def test_every_bound_parameter_is_explicitly_cast(self) -> None:
        """PostgreSQL types a bare placeholder as `unknown`.

        `:capacity - :cost` is `unknown - unknown`, which the server cannot
        resolve — it answers "could not determine data type of parameter", the
        driver raises, and the limiter fails **closed** on every request
        forever. That is a total outage that looks exactly like a healthy
        limiter from the outside, and only executing the statement against a
        real server catches it. Found by the first integration run.
        """
        fragments = (
            PostgresRateLimitStore._REFILLED,
            PostgresRateLimitStore._COST,
            PostgresRateLimitStore._TTL,
            self._source(),
        )
        blob = "\n".join(fragments)
        for name in (":capacity", ":cost", ":refill", ":ttl"):
            assert re.search(rf"CAST\(\s*{re.escape(name)}\s+AS\s+double precision\s*\)", blob), (
                f"{name} is never cast; PostgreSQL cannot infer its type"
            )

    def test_no_placeholder_is_used_in_bare_arithmetic(self) -> None:
        """The specific shape that fails: `:a - :b` with nothing to infer from."""
        blob = "\n".join(
            (
                PostgresRateLimitStore._REFILLED,
                PostgresRateLimitStore._COST,
                PostgresRateLimitStore._TTL,
                self._source(),
            )
        )
        bare = re.findall(r":\w+\s*[-+*/]\s*:\w+", blob)
        assert bare == [], f"placeholder arithmetic without a cast: {bare}"

    def test_the_store_fails_closed(self) -> None:
        """A limiter that fails open under database stress does not work when
        it is needed, which is under database stress."""
        source = self._source()
        assert "failing closed" in source
        assert re.search(r"except Exception as exc:.*allowed=False", source, re.DOTALL)

    def test_a_failure_records_its_error_class(self) -> None:
        """A generic "unavailable" hid a malformed statement.

        A network outage and a SQL type error produce identical behaviour —
        refuse everything — so the log line has to tell them apart.
        """
        source = self._source()
        assert "type(exc).__name__" in source
        assert "self.failures += 1" in source


class TestBucketBehaviour:
    """The contract both stores implement. Run here against the in-memory one."""

    def _limiter(self, per_minute: int, burst: int | None = None) -> LayeredRateLimiter:
        return LayeredRateLimiter(
            InMemoryBucketStore(),
            {LimitScope.CONVERSATION: LimitPolicy(per_minute=per_minute, burst=burst)},
        )

    async def test_requests_within_the_burst_are_allowed(self) -> None:
        limiter = self._limiter(60, burst=5)
        results = [(await limiter.check(LimitScope.CONVERSATION, "c1")).allowed for _ in range(5)]
        assert all(results)

    async def test_the_request_past_the_burst_is_refused(self) -> None:
        """The assertion the production store used to fail."""
        limiter = self._limiter(60, burst=3)
        for _ in range(3):
            await limiter.check(LimitScope.CONVERSATION, "c1")
        decision = await limiter.check(LimitScope.CONVERSATION, "c1")
        assert decision.limited
        assert decision.retry_after_seconds > 0
        assert decision.enforcement is Enforcement.SOFT_THROTTLE

    async def test_buckets_are_independent_per_key(self) -> None:
        limiter = self._limiter(60, burst=1)
        assert (await limiter.check(LimitScope.CONVERSATION, "a")).allowed
        assert (await limiter.check(LimitScope.CONVERSATION, "b")).allowed
        assert (await limiter.check(LimitScope.CONVERSATION, "a")).limited

    async def test_an_unconfigured_scope_is_unlimited_not_blocked(self) -> None:
        """A missing config entry must not silently shut down a whole scope."""
        limiter = self._limiter(60)
        assert (await limiter.check(LimitScope.WEBSITE_WRITE, "k")).allowed

    async def test_the_first_failing_layer_short_circuits(self) -> None:
        """A request already refused must not also drain the global bucket."""
        store = InMemoryBucketStore()
        limiter = LayeredRateLimiter(
            store,
            {
                LimitScope.CONVERSATION: LimitPolicy(per_minute=60, burst=1),
                LimitScope.GLOBAL: LimitPolicy(per_minute=60, burst=2),
            },
        )
        checks = [(LimitScope.CONVERSATION, "c1"), (LimitScope.GLOBAL, "all")]
        assert (await limiter.check_all(checks)).allowed
        assert (await limiter.check_all(checks)).limited
        assert (await limiter.check_all(checks)).limited
        # The global bucket had capacity 2 and only one token was ever taken,
        # so an unrelated conversation is still served.
        assert (
            await limiter.check_all([(LimitScope.CONVERSATION, "c2"), (LimitScope.GLOBAL, "all")])
        ).allowed

    async def test_tokens_refill_over_time(self) -> None:
        store = InMemoryBucketStore()
        for _ in range(2):
            await store.consume("k", capacity=2, refill_per_second=1.0, now=0.0)
        refused = await store.consume("k", capacity=2, refill_per_second=1.0, now=0.0)
        assert not refused.allowed  # type: ignore[attr-defined]
        later = await store.consume("k", capacity=2, refill_per_second=1.0, now=5.0)
        assert later.allowed  # type: ignore[attr-defined]

    @pytest.mark.parametrize("cost", [2.0, 10.0])
    async def test_a_cost_above_capacity_is_refused(self, cost: float) -> None:
        store = InMemoryBucketStore()
        state = await store.consume("k", capacity=1.0, refill_per_second=1.0, cost=cost)
        assert not state.allowed  # type: ignore[attr-defined]

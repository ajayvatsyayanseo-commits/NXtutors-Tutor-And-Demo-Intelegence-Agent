"""Circuit breaker, rate limiting and the internal signing scheme."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from demo_command_center.resilience.circuit import CircuitBreaker, CircuitOpen, State
from demo_command_center.security.rate_limit import (
    DurableLimiter,
    InProcessLimiter,
    LayeredLimiter,
    LimitScope,
    RateLimited,
)
from demo_command_center.security.signatures import (
    SignatureError,
    SignatureFailure,
    SignedRequest,
    idempotency_key,
    internal_canonical_string,
    meta_signature,
    parse_timestamp,
    sign_internal,
    verify_internal,
    verify_meta,
    verify_meta_challenge,
)
from demo_command_center.shared.clock import FrozenClock

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
SECRET = "internal-test-secret"


class FakeMonotonic:
    """A controllable clock. `time.monotonic` cannot be moved by a test."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


# ========================================================= circuit breaker


class TestCircuitBreaker:
    def breaker(self) -> tuple[CircuitBreaker, FakeMonotonic]:
        clock = FakeMonotonic()
        return (
            CircuitBreaker(name="test", failure_threshold=3, reset_seconds=30, monotonic=clock),
            clock,
        )

    def test_a_closed_breaker_admits_calls(self) -> None:
        breaker, _ = self.breaker()
        breaker.before_call()
        assert breaker.state is State.CLOSED

    def test_it_opens_after_the_threshold(self) -> None:
        breaker, _ = self.breaker()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state is State.OPEN
        with pytest.raises(CircuitOpen):
            breaker.before_call()

    def test_a_success_resets_the_failure_count(self) -> None:
        breaker, _ = self.breaker()
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.state is State.CLOSED

    def test_it_half_opens_after_the_reset_window(self) -> None:
        breaker, clock = self.breaker()
        for _ in range(3):
            breaker.record_failure()
        clock.value = 31.0
        breaker.before_call()
        assert breaker.state is State.HALF_OPEN

    def test_half_open_admits_only_the_configured_probes(self) -> None:
        """Admitting everything at once stampedes a recovering provider back over."""
        breaker, clock = self.breaker()
        for _ in range(3):
            breaker.record_failure()
        clock.value = 31.0
        breaker.before_call()
        with pytest.raises(CircuitOpen):
            breaker.before_call()

    def test_a_failed_probe_reopens_immediately(self) -> None:
        breaker, clock = self.breaker()
        for _ in range(3):
            breaker.record_failure()
        clock.value = 31.0
        breaker.before_call()
        breaker.record_failure()
        assert breaker.state is State.OPEN

    def test_a_successful_probe_closes_the_circuit(self) -> None:
        breaker, clock = self.breaker()
        for _ in range(3):
            breaker.record_failure()
        clock.value = 31.0
        breaker.before_call()
        breaker.record_success()
        assert breaker.state is State.CLOSED
        breaker.before_call()

    def test_the_retry_hint_is_useful(self) -> None:
        breaker, clock = self.breaker()
        for _ in range(3):
            breaker.record_failure()
        clock.value = 10.0
        with pytest.raises(CircuitOpen) as exc:
            breaker.before_call()
        assert 19.0 <= exc.value.retry_after <= 21.0


# ============================================================ rate limiting


class TestRateLimiting:
    async def test_a_bucket_allows_up_to_its_limit(self) -> None:
        limiter = InProcessLimiter(FrozenClock(NOW))
        for _ in range(3):
            assert (await limiter.check(LimitScope.LLM, "cv_1", limit=3, per_seconds=60)).allowed

    async def test_it_refuses_past_the_limit_and_says_when_to_retry(self) -> None:
        limiter = InProcessLimiter(FrozenClock(NOW))
        for _ in range(3):
            await limiter.check(LimitScope.LLM, "cv_1", limit=3, per_seconds=60)
        decision = await limiter.check(LimitScope.LLM, "cv_1", limit=3, per_seconds=60)
        assert not decision.allowed
        assert decision.retry_after_seconds > 0

    async def test_buckets_are_independent_per_key_and_scope(self) -> None:
        limiter = InProcessLimiter(FrozenClock(NOW))
        await limiter.check(LimitScope.LLM, "cv_1", limit=1, per_seconds=60)
        assert (await limiter.check(LimitScope.LLM, "cv_2", limit=1, per_seconds=60)).allowed
        assert (
            await limiter.check(LimitScope.WHATSAPP_SEND, "cv_1", limit=1, per_seconds=60)
        ).allowed

    async def test_the_window_resets(self) -> None:
        clock = FrozenClock(NOW)
        limiter = InProcessLimiter(clock)
        await limiter.check(LimitScope.LLM, "cv_1", limit=1, per_seconds=60)
        assert not (await limiter.check(LimitScope.LLM, "cv_1", limit=1, per_seconds=60)).allowed
        clock.advance(__import__("datetime").timedelta(seconds=61))
        assert (await limiter.check(LimitScope.LLM, "cv_1", limit=1, per_seconds=60)).allowed

    async def test_raise_for_limit_carries_the_scope(self) -> None:
        limiter = InProcessLimiter(FrozenClock(NOW))
        await limiter.check(LimitScope.PAYMENT_ORDER, "cv", limit=1, per_seconds=60)
        decision = await limiter.check(LimitScope.PAYMENT_ORDER, "cv", limit=1, per_seconds=60)
        with pytest.raises(RateLimited) as exc:
            decision.raise_for_limit()
        assert exc.value.scope is LimitScope.PAYMENT_ORDER

    async def test_the_durable_layer_fails_open_when_storage_is_down(self) -> None:
        """A blip must not take the WhatsApp path down. Limits are cost
        controls; signature verification is the security boundary, and that
        does not fail open."""

        class BrokenStore:
            async def increment_window(self, **kwargs: object) -> int:
                raise RuntimeError("database unavailable")

        limiter = DurableLimiter(BrokenStore(), FrozenClock(NOW))
        assert (await limiter.check(LimitScope.LLM, "cv", limit=1, per_seconds=60)).allowed

    async def test_the_layered_limiter_never_touches_storage_when_the_fast_layer_refuses(
        self,
    ) -> None:
        calls: list[str] = []

        class CountingStore:
            async def increment_window(self, **kwargs: object) -> int:
                calls.append("hit")
                return 1

        layered = LayeredLimiter(
            InProcessLimiter(FrozenClock(NOW)), DurableLimiter(CountingStore(), FrozenClock(NOW))
        )
        await layered.check(LimitScope.LLM, "cv", limit=1, per_seconds=60)
        await layered.check(LimitScope.LLM, "cv", limit=1, per_seconds=60)
        assert len(calls) == 1, "the over-limit call must not write"


# ============================================================== signatures


class TestSignatures:
    def request(self, **overrides: object) -> SignedRequest:
        base: dict[str, object] = {
            "method": "POST",
            "path": "/internal/handoff",
            "timestamp": int(NOW.timestamp()),
            "body": b'{"x":1}',
        }
        base.update(overrides)
        return SignedRequest(**base)  # type: ignore[arg-type]

    def verify(self, request: SignedRequest, provided: str) -> None:
        verify_internal(
            secret=SECRET,
            request=request,
            provided=provided,
            tolerance_seconds=300,
            max_body_bytes=65_536,
            now=NOW.timestamp(),
        )

    def test_a_valid_signature_verifies(self) -> None:
        request = self.request()
        self.verify(request, sign_internal(SECRET, request))

    def test_a_signature_minted_for_another_path_does_not_replay(self) -> None:
        """Otherwise a handoff signature is an authorization bypass to /ops."""
        signed = sign_internal(SECRET, self.request(path="/internal/handoff"))
        with pytest.raises(SignatureError) as exc:
            self.verify(self.request(path="/ops/demos"), signed)
        assert exc.value.reason is SignatureFailure.SIGNATURE_MISMATCH

    def test_a_signature_for_another_method_does_not_replay(self) -> None:
        signed = sign_internal(SECRET, self.request(method="POST"))
        with pytest.raises(SignatureError):
            self.verify(self.request(method="DELETE"), signed)

    def test_a_tampered_body_does_not_verify(self) -> None:
        signed = sign_internal(SECRET, self.request(body=b'{"amount":100}'))
        with pytest.raises(SignatureError):
            self.verify(self.request(body=b'{"amount":1}'), signed)

    def test_a_stale_timestamp_is_refused(self) -> None:
        request = self.request(timestamp=int(NOW.timestamp()) - 3_600)
        with pytest.raises(SignatureError) as exc:
            self.verify(request, sign_internal(SECRET, request))
        assert exc.value.reason is SignatureFailure.TIMESTAMP_OUT_OF_WINDOW

    def test_a_missing_signature_is_refused(self) -> None:
        with pytest.raises(SignatureError) as exc:
            self.verify(self.request(), "")
        assert exc.value.reason is SignatureFailure.MISSING_SIGNATURE

    def test_a_malformed_prefix_is_refused(self) -> None:
        with pytest.raises(SignatureError) as exc:
            self.verify(self.request(), "sha256=deadbeef")
        assert exc.value.reason is SignatureFailure.MALFORMED_SIGNATURE

    def test_an_empty_secret_fails_closed(self) -> None:
        with pytest.raises(SignatureError) as exc:
            sign_internal("", self.request())
        assert exc.value.reason is SignatureFailure.EMPTY_SECRET

    def test_the_canonical_string_binds_all_four_parts(self) -> None:
        canonical = internal_canonical_string("POST", "/a", 1, b"x")
        assert canonical.split("\n")[:3] == ["POST", "/a", "1"]
        assert canonical != internal_canonical_string("POST", "/a", 1, b"y")

    def test_a_malformed_timestamp_header_is_refused(self) -> None:
        with pytest.raises(SignatureError) as exc:
            parse_timestamp("not-a-number")
        assert exc.value.reason is SignatureFailure.MALFORMED_TIMESTAMP
        with pytest.raises(SignatureError) as exc:
            parse_timestamp(None)
        assert exc.value.reason is SignatureFailure.MISSING_TIMESTAMP

    def test_meta_signature_verification(self) -> None:
        body = b'{"entry":[]}'
        verify_meta(
            app_secret="app-secret",
            raw_body=body,
            provided=meta_signature("app-secret", body),
            max_body_bytes=65_536,
        )
        with pytest.raises(SignatureError):
            verify_meta(
                app_secret="app-secret",
                raw_body=body,
                provided=meta_signature("wrong-secret", body),
                max_body_bytes=65_536,
            )

    def test_the_meta_challenge_is_constant_time_and_strict(self) -> None:
        assert verify_meta_challenge(verify_token="tok", mode="subscribe", token="tok")
        assert not verify_meta_challenge(verify_token="tok", mode="unsubscribe", token="tok")
        assert not verify_meta_challenge(verify_token="tok", mode="subscribe", token="nope")
        assert not verify_meta_challenge(verify_token="tok", mode="subscribe", token=None)

    def test_idempotency_keys_are_stable_distinct_and_pii_free(self) -> None:
        first = idempotency_key("scope", "9876543210", "msg_1")
        assert first == idempotency_key("scope", "9876543210", "msg_1")
        assert first != idempotency_key("scope", "9876543210", "msg_2")
        assert "9876543210" not in first
        assert len(first) == 48

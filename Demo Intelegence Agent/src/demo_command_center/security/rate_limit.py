"""Layered rate limiting.

No Redis, so the shared bucket is a database row. That is expensive enough that
it is worth being honest about which limits need durability:

* **Per-container token bucket** (`InProcessLimiter`) — free, approximate, and
  correct enough for provider-facing concurrency where the goal is "do not
  hammer Google", not "enforce a quota exactly".
* **Durable counter** (`DurableLimiter`) — one `UPDATE ... RETURNING` against a
  fixed-window row. Used only where an over-limit request costs real money or
  real reputation: LLM calls, WhatsApp sends, payment-order creation.

A fixed window, not a sliding one. A sliding window needs either a sorted set
(Redis) or a per-event row (expensive); a fixed window can double the rate at a
boundary, which for "12 messages per minute per conversation" is a limit of 24
in the worst instant and entirely acceptable. Named so nobody has to rediscover
it: `FixedWindow`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from demo_command_center.shared.clock import Clock


class LimitScope(StrEnum):
    CONVERSATION = "conversation"
    IDENTITY = "identity"
    GLOBAL = "global"
    LLM = "llm"
    WHATSAPP_SEND = "whatsapp_send"
    PAYMENT_ORDER = "payment_order"
    GOOGLE = "google"
    GATEWAY = "gateway"
    OPS_API = "ops_api"


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    scope: LimitScope
    remaining: int
    retry_after_seconds: int = 0

    def raise_for_limit(self) -> None:
        if not self.allowed:
            raise RateLimited(self.scope, self.retry_after_seconds)


class RateLimited(Exception):
    def __init__(self, scope: LimitScope, retry_after_seconds: int) -> None:
        super().__init__(f"{scope.value} rate limit exceeded")
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


@runtime_checkable
class Limiter(Protocol):
    async def check(self, scope: LimitScope, key: str, *, limit: int, per_seconds: int) -> Decision:
        ...


@dataclass(slots=True)
class _Window:
    started_at: float
    count: int


class InProcessLimiter:
    """Per-container fixed window. Resets on cold start, and that is fine.

    Bounded on purpose: an unbounded dict keyed by conversation id is a slow
    memory leak in a long-lived warm container, and the eviction being LRU-ish
    (oldest window first) costs nothing.
    """

    def __init__(self, clock: Clock, *, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._windows: dict[tuple[str, str], _Window] = {}
        self._max_keys = max_keys

    async def check(self, scope: LimitScope, key: str, *, limit: int, per_seconds: int) -> Decision:
        now = self._clock.now().timestamp()
        bucket = (scope.value, key)
        window = self._windows.get(bucket)
        if window is None or now - window.started_at >= per_seconds:
            window = _Window(started_at=now, count=0)
            self._windows[bucket] = window
            self._evict_if_needed()
        window.count += 1
        if window.count > limit:
            retry = math.ceil(per_seconds - (now - window.started_at))
            return Decision(False, scope, remaining=0, retry_after_seconds=max(retry, 1))
        return Decision(True, scope, remaining=limit - window.count)

    def _evict_if_needed(self) -> None:
        if len(self._windows) <= self._max_keys:
            return
        oldest = sorted(self._windows.items(), key=lambda item: item[1].started_at)
        for bucket, _ in oldest[: len(self._windows) - self._max_keys]:
            self._windows.pop(bucket, None)


@runtime_checkable
class CounterStore(Protocol):
    """The durable side. One atomic increment-and-read per call."""

    async def increment_window(
        self, *, scope: str, key: str, window_start: int, ttl_seconds: int
    ) -> int:
        ...


class DurableLimiter:
    """Cross-container limit backed by a counter table.

    Fails **open** on a storage error, deliberately. A rate limiter that fails
    closed converts a database blip into a total outage of the WhatsApp path,
    and the durable limits here are cost controls rather than the security
    boundary — signature verification and authorization are, and neither of them
    fails open.
    """

    def __init__(self, store: CounterStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def check(self, scope: LimitScope, key: str, *, limit: int, per_seconds: int) -> Decision:
        now = int(self._clock.now().timestamp())
        window_start = now - (now % per_seconds)
        try:
            count = await self._store.increment_window(
                scope=scope.value,
                key=key,
                window_start=window_start,
                ttl_seconds=per_seconds * 2,
            )
        except Exception:  # noqa: BLE001 - see docstring: fail open, but loudly
            from demo_command_center.observability.logging import get_logger

            get_logger("rate_limit").warning(
                "durable limiter unavailable, failing open", extra={"dcc_scope": scope.value}
            )
            return Decision(True, scope, remaining=limit)
        if count > limit:
            return Decision(
                False,
                scope,
                remaining=0,
                retry_after_seconds=max(window_start + per_seconds - now, 1),
            )
        return Decision(True, scope, remaining=limit - count)


class LayeredLimiter:
    """Cheap layer first, durable layer only if the cheap one passed.

    The ordering is the whole point: a conversation already over its
    per-container limit never causes a database write.
    """

    def __init__(self, fast: Limiter, durable: Limiter | None = None) -> None:
        self._fast = fast
        self._durable = durable

    async def check(self, scope: LimitScope, key: str, *, limit: int, per_seconds: int) -> Decision:
        verdict = await self._fast.check(scope, key, limit=limit, per_seconds=per_seconds)
        if not verdict.allowed or self._durable is None:
            return verdict
        return await self._durable.check(scope, key, limit=limit, per_seconds=per_seconds)

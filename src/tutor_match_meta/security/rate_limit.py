"""Layered rate limiting and abuse control.

Six layers, each answering a different question, all configurable and none
hardcoded in a handler:

| Layer | Protects against |
| --- | --- |
| `CALLER` | one misbehaving upstream service |
| `CONVERSATION` | one parent (or one broken retry loop) |
| `IDENTITY` | one phone across several conversations |
| `LLM` | provider spend, checked *before* the call |
| `WEBSITE_WRITE` | hammering the Laravel API |
| `GLOBAL` | systemic runaway; the emergency brake |

Two properties matter more than the algorithm choice.

**Shared across containers.** A per-conversation bucket held in Lambda memory
gives an effective limit of `N_containers × limit`, which is not a limit. The
backing store is PostgreSQL (`PostgresRateLimitStore`), where the whole
refill-check-decrement is one atomic statement. Redis was removed deliberately —
see `cache/postgres_store.py`.

**The LLM check happens before the call, not after.** Rate-limited traffic must
not still cost money; a limiter that runs after the spend is an audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from tutor_match_meta.cache.base import Cache


class LimitScope(StrEnum):
    CALLER = "caller"
    CONVERSATION = "conversation"
    IDENTITY = "identity"
    LLM = "llm"
    WEBSITE_WRITE = "website_write"
    GLOBAL = "global"


class Enforcement(StrEnum):
    """Escalating response. One malformed message never reaches a block."""

    ALLOW = "allow"
    #: Over the limit; back off and retry. The normal case.
    SOFT_THROTTLE = "soft_throttle"
    #: Sustained abuse; refuse for a while.
    HARD_THROTTLE = "hard_throttle"
    #: Sustained abuse across windows; refuse and stop spending on this caller.
    COOLDOWN = "cooldown"
    #: A human decides. Never applied automatically from a single signal.
    HUMAN_REVIEW = "human_review"


@dataclass(frozen=True, slots=True)
class LimitPolicy:
    """Per-scope thresholds. Configuration, never literals in a handler."""

    per_minute: int
    #: Burst allowance. Defaults to the per-minute rate: people genuinely do
    #: send three messages in a row, and refusing that is worse than the abuse.
    burst: int | None = None

    @property
    def capacity(self) -> float:
        return float(self.burst if self.burst is not None else self.per_minute)

    @property
    def refill_per_second(self) -> float:
        return self.per_minute / 60.0


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    scope: LimitScope
    remaining: float
    retry_after_seconds: float
    enforcement: Enforcement = Enforcement.ALLOW

    @property
    def limited(self) -> bool:
        return not self.allowed


@runtime_checkable
class BucketStore(Protocol):
    """The shared token-bucket store. PostgreSQL in production."""

    async def consume(
        self, key: str, *, capacity: float, refill_per_second: float, cost: float = 1.0
    ) -> object: ...


class LayeredRateLimiter:
    """Checks the configured layers in order, cheapest and narrowest first.

    Ordering is deliberate: the conversation limit is checked before the global
    one so a single spammer is attributed to themselves rather than tripping the
    emergency brake for everyone.
    """

    def __init__(self, store: BucketStore, policies: dict[LimitScope, LimitPolicy]) -> None:
        self._store = store
        self._policies = policies

    async def check(self, scope: LimitScope, key: str, *, cost: float = 1.0) -> RateLimitDecision:
        policy = self._policies.get(scope)
        if policy is None or policy.per_minute <= 0:
            # No policy configured means unlimited, not zero. A missing config
            # entry must not silently block a whole scope.
            return RateLimitDecision(True, scope, float("inf"), 0.0)

        state = await self._store.consume(
            f"rl:{scope.value}:{key}",
            capacity=policy.capacity,
            refill_per_second=policy.refill_per_second,
            cost=cost,
        )
        allowed = bool(getattr(state, "allowed", False))
        remaining = float(getattr(state, "tokens", 0.0))
        retry_after = float(getattr(state, "retry_after_seconds", 1.0))

        return RateLimitDecision(
            allowed=allowed,
            scope=scope,
            remaining=remaining,
            retry_after_seconds=retry_after,
            enforcement=Enforcement.ALLOW if allowed else Enforcement.SOFT_THROTTLE,
        )

    async def check_all(self, checks: list[tuple[LimitScope, str]]) -> RateLimitDecision:
        """First failing layer wins; the rest are not consumed.

        Short-circuiting matters: consuming a global token for a request already
        refused by the conversation limit would let one spammer drain the
        emergency brake.
        """
        for scope, key in checks:
            decision = await self.check(scope, key)
            if decision.limited:
                return decision
        return RateLimitDecision(True, LimitScope.GLOBAL, float("inf"), 0.0)


def policies_from_settings(settings: object) -> dict[LimitScope, LimitPolicy]:
    """Build the policy table from configuration."""

    def value(name: str, default: int) -> int:
        return int(getattr(settings, name, default) or default)

    return {
        LimitScope.CALLER: LimitPolicy(value("rate_limit_per_caller_per_minute", 600)),
        LimitScope.CONVERSATION: LimitPolicy(value("rate_limit_per_conversation_per_minute", 12)),
        LimitScope.IDENTITY: LimitPolicy(value("rate_limit_per_identity_per_minute", 20)),
        LimitScope.LLM: LimitPolicy(value("rate_limit_llm_per_conversation_per_minute", 4)),
        LimitScope.WEBSITE_WRITE: LimitPolicy(value("rate_limit_website_write_per_minute", 30)),
        LimitScope.GLOBAL: LimitPolicy(value("rate_limit_global_per_minute", 600)),
    }


# --------------------------------------------------------------------- abuse
@dataclass(frozen=True, slots=True)
class AbuseSignal:
    kind: str
    detail: str = ""


@dataclass(slots=True)
class AbuseAssessment:
    signals: list[AbuseSignal]
    enforcement: Enforcement

    @property
    def suspicious(self) -> bool:
        return bool(self.signals)


#: Phrases that look like an attempt to harvest tutor contact details rather
#: than to find a tutor. Detected and *counted*, never used to block outright —
#: a parent legitimately asking "can I call the tutor" is not an attacker, and
#: the real defence is that the data is not in the model payload at all.
_HARVEST_MARKERS = (
    "phone number",
    "mobile number",
    "contact number",
    "whatsapp number",
    "email address",
    "email id",
    "all tutors",
    "every tutor",
    "full list",
    "database",
    "export",
    "csv",
)


class AbuseDetector:
    """Behavioural signals, escalating slowly.

    Deliberately conservative about escalation. A parent who sends a malformed
    message twice is confused, not hostile; blocking them is a worse outcome
    than serving one extra request. Only sustained, repeated signals reach
    `HUMAN_REVIEW`, and nothing here blocks permanently on its own.
    """

    def __init__(self, cache: Cache, *, window_seconds: int = 300) -> None:
        self._cache = cache
        self._window = window_seconds

    async def assess(
        self,
        conversation_ref: str,
        text: str,
        *,
        rate_limited: bool = False,
    ) -> AbuseAssessment:
        signals: list[AbuseSignal] = []
        lowered = text.lower()

        if await self._is_repeat(conversation_ref, text):
            signals.append(AbuseSignal("repeated_identical_message"))

        harvest_hits = [m for m in _HARVEST_MARKERS if m in lowered]
        if harvest_hits:
            signals.append(AbuseSignal("contact_harvest_attempt", ",".join(harvest_hits[:3])))

        if rate_limited:
            signals.append(AbuseSignal("rate_limited"))

        strikes = await self._strikes(conversation_ref, len(signals))
        return AbuseAssessment(signals=signals, enforcement=_escalate(strikes, signals))

    async def _is_repeat(self, conversation_ref: str, text: str) -> bool:
        """Near-identical resend.

        Usually means our last reply did not land, not that a second shortlist
        is wanted. Distinct from idempotency, which only catches an identical
        *event id*.
        """
        import hashlib

        fingerprint = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:24]
        key = f"flood:{conversation_ref}:{fingerprint}"
        if await self._cache.get(key) is not None:
            return True
        await self._cache.set(key, "1", ttl_seconds=self._window)
        return False

    async def _strikes(self, conversation_ref: str, new_signals: int) -> int:
        """Signals accumulated in the window. Decays with the TTL."""
        key = f"strikes:{conversation_ref}"
        current = int(await self._cache.get(key) or 0)
        if new_signals:
            current += new_signals
            await self._cache.set(key, str(current), ttl_seconds=self._window)
        return current


def _escalate(strikes: int, signals: list[AbuseSignal]) -> Enforcement:
    """Strike count to enforcement. Gradual on purpose."""
    if not signals:
        return Enforcement.ALLOW
    if strikes <= 2:
        return Enforcement.SOFT_THROTTLE
    if strikes <= 5:
        return Enforcement.HARD_THROTTLE
    if strikes <= 10:
        return Enforcement.COOLDOWN
    # Only a sustained pattern reaches a human. Never an automatic block.
    return Enforcement.HUMAN_REVIEW


class InMemoryBucketStore:
    """Single-process token bucket for tests and local runs.

    Correct within one process. Across containers it is approximate — which is
    exactly why production uses the PostgreSQL store.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}

    async def consume(
        self,
        key: str,
        *,
        capacity: float,
        refill_per_second: float,
        cost: float = 1.0,
        now: float | None = None,
    ) -> object:
        import time

        from tutor_match_meta.cache.postgres_store import BucketState

        current = now if now is not None else time.monotonic()
        tokens, updated = self._buckets.get(key, (capacity, current))
        tokens = min(capacity, tokens + max(0.0, current - updated) * refill_per_second)

        if tokens < cost:
            self._buckets[key] = (tokens, current)
            deficit = cost - tokens
            retry = deficit / refill_per_second if refill_per_second > 0 else 60.0
            return BucketState(tokens=tokens, allowed=False, retry_after_seconds=retry)

        self._buckets[key] = (tokens - cost, current)
        return BucketState(tokens=tokens - cost, allowed=True, retry_after_seconds=0.0)

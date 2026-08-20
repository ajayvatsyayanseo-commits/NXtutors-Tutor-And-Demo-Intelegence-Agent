"""Two-level cache. No Redis, and correctness never depends on either level.

**L1** — bounded, in-process, per warm Lambda. Policy documents, schemas,
template metadata, provider clients. All of it is either immutable for the life
of a deployment or cheap to re-derive.

**L2** — Demo-owned PostgreSQL rows, for things expensive to fetch and safe to
be slightly stale. Only reached on an L1 miss.

The list of what must **never** be cached as authoritative is the important part
of this module, and it is enforced rather than documented: `CacheKey` is a
closed enum, `NEVER_CACHE` names the values that would be wrong to add, and
`assert_cacheable()` refuses them. Adding "slot availability" to this cache
requires deleting a line that says not to.

Why those specific exclusions:

* **tutor availability / slot confirmation** — a cached "free" is how two
  parents get the same slot.
* **payment success / subscription activation** — a cached "paid" is how an
  unpaid customer gets a plan.
* **conversation ownership** — a cached owner is how a human gets talked over.
* **revoked permissions** — bounded to seconds, never minutes.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class CacheKey(StrEnum):
    """Everything that may be cached. A closed set, deliberately."""

    POLICY_DOCUMENT = "policy_document"
    TEMPLATE_REGISTRY = "template_registry"
    JSON_SCHEMA = "json_schema"
    PLAN_CATALOGUE = "plan_catalogue"
    REGION_AUTHORIZATION = "region_authorization"
    TUTOR_PUBLIC_PROFILE = "tutor_public_profile"
    PROVIDER_METADATA = "provider_metadata"
    CIRCUIT_STATE = "circuit_state"


#: Things that would be wrong to cache as authoritative, and why. Named so that
#: adding one is a deliberate deletion rather than an oversight.
NEVER_CACHE: dict[str, str] = {
    "tutor_availability": "a cached 'free' double-books a tutor",
    "slot_confirmation": "a cached hold is not a hold",
    "payment_status": "a cached 'paid' activates an unpaid plan",
    "subscription_activation": "same, one step later",
    "conversation_ownership": "a cached owner talks over a human",
    "message_idempotency": "a cached 'not sent' sends twice",
}

#: Per-key TTL ceilings. Authorization is deliberately the shortest: a revoked
#: sub-admin keeping regional access for a minute is a real, bounded exposure,
#: and the bound is what makes it acceptable.
TTL_CEILINGS: dict[CacheKey, int] = {
    CacheKey.POLICY_DOCUMENT: 86_400,
    CacheKey.TEMPLATE_REGISTRY: 86_400,
    CacheKey.JSON_SCHEMA: 86_400,
    CacheKey.PLAN_CATALOGUE: 300,
    CacheKey.REGION_AUTHORIZATION: 60,
    CacheKey.TUTOR_PUBLIC_PROFILE: 600,
    CacheKey.PROVIDER_METADATA: 3_600,
    CacheKey.CIRCUIT_STATE: 30,
}


class NotCacheable(ValueError):
    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"{name} must not be cached: {reason}")
        self.name = name


def assert_cacheable(name: str) -> None:
    reason = NEVER_CACHE.get(name)
    if reason is not None:
        raise NotCacheable(name, reason)


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float


class L1Cache:
    """Bounded LRU with per-entry TTL. Per warm container; lost on cold start.

    Bounded on purpose: an unbounded dict keyed by anything user-supplied is a
    slow memory leak in a long-lived warm container, and Lambda kills the
    function rather than the cache.
    """

    def __init__(self, *, max_entries: int = 512, monotonic: Any = None) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._max = max_entries
        self._now = monotonic or time.monotonic
        self.hits = 0
        self.misses = 0

    def _key(self, kind: CacheKey, key: str) -> str:
        return f"{kind.value}:{key}"

    def get(self, kind: CacheKey, key: str) -> Any | None:
        composite = self._key(kind, key)
        entry = self._entries.get(composite)
        if entry is None:
            self.misses += 1
            return None
        if self._now() >= entry.expires_at:
            del self._entries[composite]
            self.misses += 1
            return None
        self._entries.move_to_end(composite)
        self.hits += 1
        return entry.value

    def put(self, kind: CacheKey, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        ceiling = TTL_CEILINGS[kind]
        # The ceiling is a ceiling, not a default. A caller asking for a longer
        # TTL than the kind permits gets the kind's answer.
        ttl = min(ttl_seconds, ceiling) if ttl_seconds is not None else ceiling
        composite = self._key(kind, key)
        self._entries[composite] = _Entry(value=value, expires_at=self._now() + ttl)
        self._entries.move_to_end(composite)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def invalidate(self, kind: CacheKey, key: str | None = None) -> int:
        """Drop one entry, or every entry of a kind. Returns how many went."""
        if key is not None:
            return int(self._entries.pop(self._key(kind, key), None) is not None)
        prefix = f"{kind.value}:"
        doomed = [k for k in self._entries if k.startswith(prefix)]
        for k in doomed:
            del self._entries[k]
        return len(doomed)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    def stats(self) -> dict[str, float]:
        return {
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
        }


@runtime_checkable
class L2Store(Protocol):
    """The durable tier. A Demo-owned table, never a new service."""

    async def read(self, kind: str, key: str) -> dict[str, Any] | None: ...

    async def write(self, kind: str, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...

    async def purge(self, kind: str, key: str | None = None) -> int: ...


class LayeredCache:
    """L1 in front of L2. A miss in both returns None — never a guess."""

    def __init__(self, l1: L1Cache, l2: L2Store | None = None) -> None:
        self._l1 = l1
        self._l2 = l2

    async def get(self, kind: CacheKey, key: str) -> Any | None:
        hit = self._l1.get(kind, key)
        if hit is not None:
            return hit
        if self._l2 is None:
            return None
        value = await self._l2.read(kind.value, key)
        if value is not None:
            # Populate L1 so the next warm invocation avoids the round trip.
            self._l1.put(kind, key, value)
        return value

    async def put(
        self, kind: CacheKey, key: str, value: Any, *, ttl_seconds: int | None = None
    ) -> None:
        ttl = min(ttl_seconds, TTL_CEILINGS[kind]) if ttl_seconds else TTL_CEILINGS[kind]
        self._l1.put(kind, key, value, ttl_seconds=ttl)
        if self._l2 is not None and isinstance(value, dict):
            await self._l2.write(kind.value, key, value, ttl)

    async def invalidate(self, kind: CacheKey, key: str | None = None) -> None:
        """Both tiers, always. Invalidating one is worse than caching neither —
        the stale tier repopulates the fresh one on the next miss."""
        self._l1.invalidate(kind, key)
        if self._l2 is not None:
            await self._l2.purge(kind.value, key)

    def stats(self) -> dict[str, float]:
        return self._l1.stats()

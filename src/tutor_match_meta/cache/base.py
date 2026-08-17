"""Cache interface and tiering.

Business logic depends on `Cache`, never on a concrete backend. There is no
Redis here: ElastiCache is always-on hourly billing for a workload that scales
to zero between messages, so the shared tier is PostgreSQL.

Three tiers, with sharply different trust:

* **L1** in-process, warm-Lambda only. Disposable, invisible to other containers,
  and given a short TTL because it cannot be invalidated across containers.
* **L2** PostgreSQL `kv_entry`. Shared across containers, invalidatable.
* **L3** PostgreSQL. Not a cache — the canonical source every miss falls back to.

The rule the whole module exists to enforce: **the cache is never the system of
record.** Every entry has a TTL, and every read path works with the cache empty.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """Minimal async cache surface."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def clear_prefix(self, prefix: str) -> int:
        """Invalidate a whole namespace. Returns how many entries were removed."""
        ...


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


@dataclass(slots=True)
class _Entry:
    value: str
    expires_at: float


class InMemoryCache:
    """LRU + TTL cache for local runs, tests and the L1 tier.

    Bounded on purpose: an unbounded dict in a warm Lambda container is a slow
    memory leak that shows up as an OOM weeks later.
    """

    def __init__(self, *, max_entries: int = 2_000) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._max = max_entries
        self.stats = CacheStats()

    async def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[key]
            self.stats.misses += 1
            return None
        self._entries.move_to_end(key)
        self.stats.hits += 1
        return entry.value

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._entries[key] = _Entry(value=value, expires_at=time.monotonic() + ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
            self.stats.evictions += 1

    async def delete(self, key: str) -> None:
        self._entries.pop(key, None)

    async def clear_prefix(self, prefix: str) -> int:
        doomed = [k for k in self._entries if k.startswith(prefix)]
        for key in doomed:
            del self._entries[key]
        return len(doomed)


class NullCache:
    """Caches nothing. Used to prove every path works on a cold cache."""

    def __init__(self) -> None:
        self.stats = CacheStats()

    async def get(self, key: str) -> str | None:
        self.stats.misses += 1
        return None

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def clear_prefix(self, prefix: str) -> int:
        return 0


class TieredCache:
    """L1 in-process in front of L2 shared.

    An L2 hit backfills L1 with a **shortened** TTL. L1 cannot receive an
    invalidation from another container, so its entries must expire on their own
    quickly enough that a policy change or a tutor deactivation cannot linger.
    """

    #: Hard ceiling on how long anything may sit in the un-invalidatable tier.
    L1_MAX_TTL_SECONDS = 30

    def __init__(self, l1: Cache, l2: Cache) -> None:
        self._l1 = l1
        self._l2 = l2

    async def get(self, key: str) -> str | None:
        value = await self._l1.get(key)
        if value is not None:
            return value
        value = await self._l2.get(key)
        if value is not None:
            await self._l1.set(key, value, ttl_seconds=self.L1_MAX_TTL_SECONDS)
        return value

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        await self._l2.set(key, value, ttl_seconds=ttl_seconds)
        await self._l1.set(key, value, ttl_seconds=min(ttl_seconds, self.L1_MAX_TTL_SECONDS))

    async def delete(self, key: str) -> None:
        await self._l1.delete(key)
        await self._l2.delete(key)

    async def clear_prefix(self, prefix: str) -> int:
        removed = await self._l2.clear_prefix(prefix)
        await self._l1.clear_prefix(prefix)
        return removed


class CacheKeys:
    """Every cache key in one place, namespaced and versioned.

    Namespacing makes `clear_prefix` meaningful; the version segment makes a
    schema change a rename rather than a migration, so a deploy cannot read old
    entries in a new shape.
    """

    VERSION = "v1"

    @staticmethod
    def _key(namespace: str, *parts: str) -> str:
        return ":".join([CacheKeys.VERSION, namespace, *(p for p in parts if p)])

    @staticmethod
    def candidate_pool(policy_ref: str, fingerprint: str) -> str:
        return CacheKeys._key("pool", policy_ref, fingerprint)

    @staticmethod
    def tutor(tutor_id: str) -> str:
        return CacheKeys._key("tutor", tutor_id)

    @staticmethod
    def geo(pincode_or_locality: str) -> str:
        return CacheKeys._key("geo", pincode_or_locality.lower())

    @staticmethod
    def rag(query_fingerprint: str) -> str:
        return CacheKeys._key("rag", query_fingerprint)

    @staticmethod
    def locality_index(city: str) -> str:
        return CacheKeys._key("locality", city.lower())


#: TTLs by namespace. Availability and account status change; subject mappings
#: and geocodes do not. Nothing sensitive is cached at any TTL.
DEFAULT_TTLS: dict[str, int] = {
    "pool": 120,  # short: a tutor can deactivate at any moment
    "tutor": 300,
    "geo": 86_400,  # a pincode centroid does not move
    "rag": 900,
    "locality": 3_600,
}

#: Namespaces that must never be cached. Enforced by
#: tests/security/test_cache_hygiene.py.
NEVER_CACHE: frozenset[str] = frozenset({"secret", "otp", "password", "token", "credential"})


def ttl_for(namespace: str, *, default: int) -> int:
    if namespace in NEVER_CACHE:
        raise ValueError(f"refusing to cache sensitive namespace {namespace!r}")
    return DEFAULT_TTLS.get(namespace, default)

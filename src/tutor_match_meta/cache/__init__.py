"""Cache tiers behind one interface. Never the system of record."""

from tutor_match_meta.cache.base import (
    DEFAULT_TTLS,
    NEVER_CACHE,
    Cache,
    CacheKeys,
    CacheStats,
    InMemoryCache,
    NullCache,
    TieredCache,
    ttl_for,
)

__all__ = [
    "DEFAULT_TTLS",
    "NEVER_CACHE",
    "Cache",
    "CacheKeys",
    "CacheStats",
    "InMemoryCache",
    "NullCache",
    "TieredCache",
    "build_cache",
    "ttl_for",
]


def build_cache(backend: str, shared: Cache | None = None) -> Cache:
    """Construct the configured cache.

    `postgres` puts an in-process L1 in front of the shared PostgreSQL store, so
    read-mostly data stays a dict lookup and only genuinely shared state pays a
    round trip. There is no Redis backend: see cache/postgres_store.py for why
    ElastiCache was removed.
    """
    if backend == "postgres" and shared is not None:
        return TieredCache(InMemoryCache(max_entries=500), shared)
    return InMemoryCache()

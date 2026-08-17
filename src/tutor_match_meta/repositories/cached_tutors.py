"""A caching decorator over `TutorSearchPort`.

The candidate query is the most expensive read on the WhatsApp path and the
most repeated: a conversation that refines "class 10 maths" into "class 10
maths CBSE" into "class 10 maths CBSE after 7pm" runs three near-identical
queries in ninety seconds, and a busy locality produces the same query shape
across many parents at once.

Three things make this safe rather than merely fast.

**The TTL is short and the reason is written down.** A tutor can deactivate,
raise a fee, or fill their last slot at any moment. 120 seconds is chosen so a
deactivation is visible within roughly one conversational turn, which is the
same order as the projection's own sync interval — caching longer would make
the cache, not the projection, the thing that decides how stale an answer can
be.

**The cache is never the system of record.** Every read path works with the
cache empty, a decode failure is a miss, and a store outage is a miss. The
`NullCache` in the test suite exercises exactly that.

**Freshness survives the round trip.** A `TutorCandidate` carries a `freshness`
enum computed against *now*. Caching the computed value would let a row cached
as FRESH be served as FRESH long after it aged out, so freshness is recomputed
on read from the stored `synced_at` rather than restored from the entry.

Explicit-tutor lookups (`public_ref`) are deliberately **not** cached: they are
rare, they are usually a parent asking about one specific tutor, and a stale
answer there is far more visible than a stale pool.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError

from tutor_match_meta.cache.base import Cache, CacheKeys, ttl_for
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.repositories.ports import CandidateQuery, TutorSearchPort

logger = get_logger("repositories.cache")

_ADAPTER: TypeAdapter[list[TutorCandidate]] = TypeAdapter(list[TutorCandidate])

#: Entries above this are not worth the round trip or the row size. A pool this
#: large also means the query was under-constrained, which is its own problem.
MAX_CACHED_POOL = 200


class CachedTutorRepository:
    """Wraps any `TutorSearchPort` with a short-TTL pool cache."""

    def __init__(
        self,
        inner: TutorSearchPort,
        cache: Cache,
        *,
        policy_ref: str = "default",
        fresh_hours: int = 6,
        aging_hours: int = 24,
        ttl_seconds: int | None = None,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._policy_ref = policy_ref
        self._fresh_hours = fresh_hours
        self._aging_hours = aging_hours
        self._ttl = ttl_seconds if ttl_seconds is not None else ttl_for("pool", default=120)
        self.hits = 0
        self.misses = 0
        self.errors = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0

    async def search(self, query: CandidateQuery) -> list[TutorCandidate]:
        if query.public_ref:
            # Never cached. See the module docstring.
            return await self._inner.search(query)

        key = CacheKeys.candidate_pool(self._policy_ref, query.fingerprint())
        cached = await self._safe_get(key)
        if cached is not None:
            self.hits += 1
            return cached

        self.misses += 1
        pool = await self._inner.search(query)
        if 0 < len(pool) <= MAX_CACHED_POOL:
            # An empty pool is deliberately not cached: "no tutors found" is
            # exactly the answer most likely to change when the sync job lands,
            # and caching it would keep telling a parent no for two minutes
            # after the fix.
            await self._safe_set(key, pool)
        return pool

    async def get(self, tutor_id: str) -> TutorCandidate | None:
        return await self._inner.get(tutor_id)

    async def get_by_public_ref(self, public_ref: str) -> TutorCandidate | None:
        return await self._inner.get_by_public_ref(public_ref)

    async def invalidate(self) -> int:
        """Drop every cached pool. Used by the sync job and by an operator."""
        return await self._cache.clear_prefix(CacheKeys.candidate_pool(self._policy_ref, ""))

    # ------------------------------------------------------------- internals
    async def _safe_get(self, key: str) -> list[TutorCandidate] | None:
        try:
            raw = await self._cache.get(key)
        except Exception:
            self.errors += 1
            logger.warning("pool cache read failed; falling through to the database")
            return None
        if raw is None:
            return None
        try:
            candidates = _ADAPTER.validate_json(raw)
        except ValidationError:
            # A shape change between deploys. Treat it as a miss rather than
            # crashing a parent's turn; the entry expires on its own.
            self.errors += 1
            logger.info("discarding a pool cache entry in an old shape")
            return None
        return [self._refresh(c) for c in candidates]

    async def _safe_set(self, key: str, pool: list[TutorCandidate]) -> None:
        try:
            await self._cache.set(key, _ADAPTER.dump_json(pool).decode(), ttl_seconds=self._ttl)
        except Exception:
            self.errors += 1
            logger.warning("pool cache write failed; continuing uncached")

    def _refresh(self, candidate: TutorCandidate) -> TutorCandidate:
        """Recompute freshness against now, never trust the cached value."""
        from tutor_match_meta.orchestration.orchestrator import freshness_for

        freshness = freshness_for(
            candidate.synced_at,
            now=datetime.now(UTC),
            fresh_hours=self._fresh_hours,
            aging_hours=self._aging_hours,
        )
        if freshness is candidate.freshness:
            return candidate
        return candidate.model_copy(update={"freshness": freshness})


__all__ = ["MAX_CACHED_POOL", "CachedTutorRepository"]

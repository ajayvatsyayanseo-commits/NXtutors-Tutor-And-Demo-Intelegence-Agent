"""The cache must never become the system of record, and never hold a secret.

Referenced by `cache/base.py`. Three properties:

* **Nothing sensitive is cacheable.** `ttl_for` refuses the namespaces that
  would hold a credential, and no key builder can produce one.
* **Every read path works cold.** Proven by running the full turn against
  `NullCache`, which caches nothing at all.
* **A cached tutor pool cannot outlive its freshness.** The freshness enum is
  recomputed on read rather than restored from the entry, so a row cached as
  FRESH is not still FRESH an hour later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tutor_match_meta.cache.base import (
    DEFAULT_TTLS,
    NEVER_CACHE,
    CacheKeys,
    InMemoryCache,
    NullCache,
    TieredCache,
    ttl_for,
)
from tutor_match_meta.contracts.common import Freshness
from tutor_match_meta.repositories.cached_tutors import CachedTutorRepository
from tutor_match_meta.repositories.memory_store import InMemoryTutorRepository
from tutor_match_meta.repositories.ports import CandidateQuery

pytestmark = pytest.mark.security


class TestNothingSensitiveIsCached:
    @pytest.mark.parametrize("namespace", sorted(NEVER_CACHE))
    def test_a_sensitive_namespace_is_refused(self, namespace: str) -> None:
        with pytest.raises(ValueError, match="refusing to cache"):
            ttl_for(namespace, default=60)

    def test_every_declared_namespace_has_a_bounded_ttl(self) -> None:
        """An unbounded entry is a stale answer waiting to be served."""
        assert all(0 < ttl <= 86_400 for ttl in DEFAULT_TTLS.values())

    def test_no_key_builder_embeds_a_raw_identifier(self) -> None:
        """Keys are hashes and canonical labels, never a phone or a message."""
        keys = [
            CacheKeys.candidate_pool("policy.v1", "a" * 24),
            CacheKeys.tutor("NXT10001"),
            CacheKeys.geo("122003"),
            CacheKeys.rag("f" * 24),
            CacheKeys.locality_index("Gurugram"),
        ]
        for key in keys:
            assert key.startswith("v1:"), "keys must be version-namespaced"
            assert "9876543210" not in key
            assert "@" not in key

    def test_the_key_version_makes_a_shape_change_a_rename(self) -> None:
        assert CacheKeys.VERSION in CacheKeys.tutor("x")


class TestEveryPathWorksCold:
    async def test_a_full_turn_succeeds_against_a_cache_that_stores_nothing(
        self, turn_deps, orchestrator
    ) -> None:
        from datetime import datetime as dt

        from tutor_match_meta.contracts.inbound import (
            InboundEnvelope,
            InboundKind,
            WhatsAppTurnV1,
        )
        from tutor_match_meta.orchestration.turn_service import TurnService

        turn_deps.cache = NullCache()
        service = TurnService(turn_deps)
        result = await service.handle(
            InboundEnvelope(
                kind=InboundKind.WHATSAPP_TURN,
                trace_id="cold",
                conversation_id="c-cold",
                dedup_key="cold:1",
                received_at=dt.now(UTC),
                source_agent="test",
                payload=WhatsAppTurnV1(
                    event_id="e",
                    conversation_id="c-cold",
                    provider_message_id="m",
                    text="class 10 cbse maths gurgaon home tuition",
                ),
            )
        )
        assert result.matched

    async def test_the_pool_cache_falls_through_when_the_store_raises(self, tutors: list) -> None:
        class ExplodingCache(NullCache):
            async def get(self, key: str) -> str | None:
                raise RuntimeError("cache is down")

            async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
                raise RuntimeError("cache is down")

        repo = CachedTutorRepository(InMemoryTutorRepository(tutors), ExplodingCache())
        pool = await repo.search(CandidateQuery(limit=5))
        assert pool, "a cache outage must not empty the candidate pool"
        assert repo.errors >= 1


class TestPoolCacheCorrectness:
    async def test_a_second_identical_query_is_served_from_cache(self, tutors: list) -> None:
        repo = CachedTutorRepository(InMemoryTutorRepository(tutors), InMemoryCache())
        query = CandidateQuery(limit=5, subjects=("Mathematics",))
        first = await repo.search(query)
        second = await repo.search(query)
        assert [t.tutor_id for t in first] == [t.tutor_id for t in second]
        assert repo.hits == 1 and repo.misses == 1

    async def test_an_empty_pool_is_never_cached(self, tutors: list) -> None:
        """'No tutors found' is the answer most likely to change on the next sync."""
        repo = CachedTutorRepository(InMemoryTutorRepository([]), InMemoryCache())
        query = CandidateQuery(limit=5, subjects=("Astrophysics",))
        await repo.search(query)
        await repo.search(query)
        assert repo.hits == 0, "an empty pool was cached"

    async def test_an_explicit_tutor_lookup_is_never_cached(self, tutors: list) -> None:
        repo = CachedTutorRepository(InMemoryTutorRepository(tutors), InMemoryCache())
        query = CandidateQuery(limit=1, public_ref=tutors[0].public_ref)
        await repo.search(query)
        await repo.search(query)
        assert repo.hits == 0 and repo.misses == 0

    async def test_freshness_is_recomputed_not_restored(self, tutors: list) -> None:
        """The property that stops a cache entry outliving its own freshness.

        A row is cached while FRESH, then read back after its `synced_at` has
        aged past the window. It must come back AGING, because freshness is a
        function of *now*, not a stored field.
        """
        stale_sync = datetime.now(UTC) - timedelta(hours=10)
        aged = [
            tutors[0].model_copy(update={"synced_at": stale_sync, "freshness": Freshness.FRESH})
        ]
        repo = CachedTutorRepository(
            InMemoryTutorRepository(aged), InMemoryCache(), fresh_hours=6, aging_hours=24
        )
        query = CandidateQuery(limit=5)
        await repo.search(query)  # populate
        served = await repo.search(query)  # from cache
        assert repo.hits == 1
        assert served[0].freshness is Freshness.AGING

    async def test_invalidation_clears_the_namespace(self, tutors: list) -> None:
        cache = InMemoryCache()
        repo = CachedTutorRepository(InMemoryTutorRepository(tutors), cache)
        await repo.search(CandidateQuery(limit=5))
        assert await repo.invalidate() >= 1
        await repo.search(CandidateQuery(limit=5))
        assert repo.hits == 0


class TestTieredCache:
    async def test_l1_ttl_is_capped_below_l2(self) -> None:
        """L1 cannot be invalidated across containers, so it must expire fast."""
        l1, l2 = InMemoryCache(), InMemoryCache()
        tiered = TieredCache(l1, l2)
        await tiered.set("k", "v", ttl_seconds=3_600)
        assert await l1.get("k") == "v"
        assert TieredCache.L1_MAX_TTL_SECONDS <= 30

    async def test_a_delete_reaches_both_tiers(self) -> None:
        l1, l2 = InMemoryCache(), InMemoryCache()
        tiered = TieredCache(l1, l2)
        await tiered.set("k", "v", ttl_seconds=60)
        await tiered.delete("k")
        assert await l1.get("k") is None
        assert await l2.get("k") is None

"""Token budgets, model routing, embedding skips, geocoding caps.

Every one of these is a *cost* control, and cost controls share a failure mode:
they are easy to implement and easy to leave off the path. So each test asserts
the control **refuses**, not merely that it exists.
"""

from __future__ import annotations

import pytest

from tutor_match_meta.integrations.llm.provider import (
    LLMBudgetExceeded,
    LLMPaused,
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelTier,
    TokenBudget,
)
from tutor_match_meta.integrations.llm.routing import (
    GuardedProvider,
    ModelRouting,
    Purpose,
    UsageLedger,
    routing_from_settings,
)

ROUTING = ModelRouting(
    extraction="gpt-4o-mini",
    escalation="gpt-4o",
    explanation="gpt-4o-mini",
    embedding="text-embedding-3-small",
)


def request(purpose: str = Purpose.EXTRACTION.value) -> LLMRequest:
    return LLMRequest(
        purpose=purpose,
        system_prompt="system",
        user_content="user",
        schema={"type": "object"},
        schema_name="S",
        prompt_version="extraction.v1",
    )


class CountingProvider:
    name = "counting"

    def __init__(self, *, prompt_tokens: int = 100, completion_tokens: int = 50) -> None:
        self.calls = 0
        self._p, self._c = prompt_tokens, completion_tokens

    async def structured(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            data={},
            usage=LLMUsage(
                provider=self.name,
                model="gpt-4o-mini",
                tier=req.tier,
                prompt_tokens=self._p,
                completion_tokens=self._c,
                cached_prompt_tokens=40,
            ),
        )


class TestTokenBudget:
    def test_the_token_ceiling_refuses(self) -> None:
        budget = TokenBudget(limit=100)
        budget.record(LLMUsage("p", "m", ModelTier.FAST, prompt_tokens=90, completion_tokens=5))
        with pytest.raises(LLMBudgetExceeded):
            budget.assert_affordable(50)

    def test_the_per_turn_call_ceiling_refuses(self) -> None:
        """The realistic runaway is call count, not token size."""
        budget = TokenBudget(limit=1_000_000, max_calls_per_turn=2)
        for _ in range(2):
            budget.record(LLMUsage("p", "m", ModelTier.FAST, prompt_tokens=1))
        with pytest.raises(LLMBudgetExceeded, match="turn call budget"):
            budget.assert_call_allowed()

    def test_begin_turn_resets_only_the_turn_counter(self) -> None:
        budget = TokenBudget(limit=1_000_000, max_calls_per_turn=1)
        budget.record(LLMUsage("p", "m", ModelTier.FAST, prompt_tokens=1))
        budget.begin_turn()
        budget.assert_call_allowed()  # allowed again this turn
        assert budget.calls == 1, "the conversation total must not reset"

    def test_the_conversation_call_ceiling_refuses(self) -> None:
        budget = TokenBudget(limit=1_000_000, max_calls_per_turn=99, max_calls_per_conversation=3)
        for _ in range(3):
            budget.record(LLMUsage("p", "m", ModelTier.FAST, prompt_tokens=1))
        with pytest.raises(LLMBudgetExceeded, match="conversation call budget"):
            budget.assert_call_allowed()

    def test_the_escalation_ceiling_refuses(self) -> None:
        budget = TokenBudget(limit=1_000_000, max_escalations=1)
        budget.record(LLMUsage("p", "m", ModelTier.REASONING, prompt_tokens=1))
        with pytest.raises(LLMBudgetExceeded, match="escalation budget"):
            budget.assert_call_allowed(tier=ModelTier.REASONING)
        # The cheap tier is still available — an exhausted escalation budget
        # must not disable ordinary extraction.
        budget.assert_call_allowed(tier=ModelTier.FAST)


class TestGuardedProvider:
    async def test_a_pause_refuses_before_the_provider_is_touched(self) -> None:
        inner = CountingProvider()

        async def paused() -> bool:
            return True

        guarded = GuardedProvider(
            inner,
            routing=ROUTING,
            budget=TokenBudget(limit=10_000),
            ledger=UsageLedger(),
            pause_check=paused,
        )
        with pytest.raises(LLMPaused):
            await guarded.structured(request())
        assert inner.calls == 0, "a paused provider was still called"

    async def test_a_rate_limit_refuses_before_the_provider_is_touched(self) -> None:
        """The whole point of checking before the spend (§8)."""
        inner = CountingProvider()

        async def refuse(key: str) -> bool:
            return False

        guarded = GuardedProvider(
            inner,
            routing=ROUTING,
            budget=TokenBudget(limit=10_000),
            ledger=UsageLedger(),
            rate_check=refuse,
        )
        with pytest.raises(LLMRateLimited):
            await guarded.structured(request())
        assert inner.calls == 0, "rate-limited traffic still cost money"

    async def test_an_exhausted_budget_refuses_before_the_provider_is_touched(self) -> None:
        inner = CountingProvider()
        budget = TokenBudget(limit=10_000, max_calls_per_turn=0)
        guarded = GuardedProvider(inner, routing=ROUTING, budget=budget, ledger=UsageLedger())
        with pytest.raises(LLMBudgetExceeded):
            await guarded.structured(request())
        assert inner.calls == 0

    async def test_usage_and_cost_are_recorded_on_success(self) -> None:
        ledger = UsageLedger(conversation_ref="cv_abc")
        guarded = GuardedProvider(
            CountingProvider(), routing=ROUTING, budget=TokenBudget(limit=10_000), ledger=ledger
        )
        await guarded.structured(request())
        assert ledger.total_tokens == 150
        assert ledger.cost_micros > 0
        assert ledger.cache.hit_rate == pytest.approx(0.4)

    async def test_usage_is_recorded_on_failure_too(self) -> None:
        """A call that timed out after retries still consumed real capacity."""

        class Failing:
            name = "failing"

            async def structured(self, req: LLMRequest) -> LLMResponse:
                raise LLMRateLimited("429")

        ledger = UsageLedger()
        guarded = GuardedProvider(
            Failing(), routing=ROUTING, budget=TokenBudget(limit=10_000), ledger=ledger
        )
        with pytest.raises(LLMRateLimited):
            await guarded.structured(request())
        assert len(ledger.entries) == 1
        assert ledger.entries[0].error_code == "LLMRateLimited"

    async def test_the_ledger_holds_no_prompt_or_conversation_id(self) -> None:
        ledger = UsageLedger(conversation_ref="cv_abc")
        guarded = GuardedProvider(
            CountingProvider(), routing=ROUTING, budget=TokenBudget(limit=10_000), ledger=ledger
        )
        await guarded.structured(request())
        blob = repr(ledger)
        assert "system" not in blob and "user" not in blob


class TestModelRouting:
    def test_every_purpose_resolves_to_a_configured_model(self) -> None:
        for purpose in Purpose:
            assert ROUTING.model_for(purpose)

    def test_only_escalation_reaches_the_reasoning_tier(self) -> None:
        expensive = [p for p in Purpose if ROUTING.tier_for(p) is ModelTier.REASONING]
        assert expensive == [Purpose.ESCALATION]

    def test_settings_fall_back_to_the_tier_aliases(self) -> None:
        class Legacy:
            llm_tier1_model = "gpt-4o-mini"
            llm_tier2_model = "gpt-4o"

        routing = routing_from_settings(Legacy())
        assert routing.extraction == "gpt-4o-mini"
        assert routing.escalation == "gpt-4o"

    def test_no_model_id_is_hardcoded_outside_configuration(self) -> None:
        """§16: model ids are configuration, not scattered literals."""
        import re
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src" / "tutor_match_meta"
        allowed = {
            # Where model ids are legitimately named.
            "config/settings.py",  # the configuration itself
            "integrations/llm/routing.py",  # purpose -> model, and its defaults
            "prompts/registry.py",  # model_compatibility, an evaluation record
            # Price tables are *keyed* by model id; that is not routing, and
            # splitting the key from the rate would make the table unreadable.
            "observability/metrics.py",
            "rag/embeddings.py",
        }
        pattern = re.compile(r"[\"'](?:gpt-[0-9]|text-embedding-|o[13]-)[\w.-]*[\"']")
        offenders = [
            path.relative_to(src).as_posix()
            for path in src.rglob("*.py")
            if path.relative_to(src).as_posix() not in allowed
            and pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"model ids hardcoded in {offenders}"


class TestEmbeddingCostControl:
    async def test_an_unchanged_chunk_is_never_re_embedded(self) -> None:
        from tutor_match_meta.rag.embeddings import (
            InMemoryEmbeddingLedger,
            embed_changed,
        )
        from tutor_match_meta.rag.pipeline import DocumentKind, SourceDocument, chunk_document

        class Backend:
            model = "text-embedding-3-small"
            dimensions = 1536

            def __init__(self) -> None:
                self.calls = 0

            async def embed(self, texts: list[str]) -> list[list[float]]:
                self.calls += 1
                return [[0.0] * 4 for _ in texts]

        chunks = chunk_document(
            SourceDocument(
                document_id="d1",
                title="Fee policy",
                source="internal",
                kind=DocumentKind.POLICY,
                content="Gurugram tuition guidance.\n\nSecond clause about scheduling.",
            )
        )
        backend, ledger = Backend(), InMemoryEmbeddingLedger()

        first = await embed_changed(chunks, backend=backend, ledger=ledger)
        assert first.embedded == len(chunks)
        assert first.cost_micros >= 0

        second = await embed_changed(chunks, backend=backend, ledger=ledger)
        assert second.embedded == 0
        assert second.skipped_unchanged == len(chunks)
        assert backend.calls == 1, "an unchanged corpus was re-embedded"

    @pytest.mark.parametrize(
        "content",
        [
            "Your OTP is 448211, valid for 10 minutes",
            "api_key: sk-abcdefghijklmnop",
            "password: hunter2 for the admin panel",
        ],
    )
    def test_credential_like_content_is_refused(self, content: str) -> None:
        from tutor_match_meta.rag.embeddings import refuse_reason

        assert refuse_reason(content) is not None

    def test_conversation_turns_are_refused(self) -> None:
        """A vector index is durable and hard to redact; a parent's message
        does not belong in one."""
        from tutor_match_meta.rag.embeddings import refuse_reason

        assert refuse_reason("hi I need a maths tutor", kind="conversation_turn") is not None

    def test_ordinary_policy_text_is_embeddable(self) -> None:
        from tutor_match_meta.rag.embeddings import refuse_reason

        assert refuse_reason("Home tuition in Gurugram is billed per session.") is None


class TestGeocodingCost:
    async def test_a_repeated_lookup_is_served_from_cache(self) -> None:
        from tutor_match_meta.cache import InMemoryCache
        from tutor_match_meta.contracts.tutor import GeoPoint
        from tutor_match_meta.integrations.geo import BudgetedGeocoder

        class Backend:
            name = "test"

            def __init__(self) -> None:
                self.calls = 0

            async def lookup(self, key: str, granularity: str) -> GeoPoint | None:
                self.calls += 1
                return GeoPoint(latitude=28.4, longitude=77.0, granularity=granularity)

        backend = Backend()
        geo = BudgetedGeocoder(backend, cache=InMemoryCache(), max_calls_per_turn=5)
        await geo.resolve(pincode="122003", locality=None, city=None)
        await geo.resolve(pincode="122003", locality=None, city=None)
        assert backend.calls == 1
        assert geo.stats.cache_hits == 1

    async def test_the_per_turn_cap_refuses_further_lookups(self) -> None:
        """One confused parse must not become an unbounded paid bill."""
        from tutor_match_meta.cache import InMemoryCache
        from tutor_match_meta.integrations.geo import BudgetedGeocoder

        class Backend:
            name = "test"

            def __init__(self) -> None:
                self.calls = 0

            async def lookup(self, key: str, granularity: str) -> None:
                self.calls += 1
                return None

        backend = Backend()
        geo = BudgetedGeocoder(backend, cache=InMemoryCache(), max_calls_per_turn=1)
        for pincode in ("122001", "122002", "122003", "122004"):
            await geo.resolve(pincode=pincode, locality=None, city=None)
        assert backend.calls == 1
        assert geo.stats.refused_over_budget >= 1

    async def test_an_unknown_location_returns_none_not_a_guess(self) -> None:
        """A guessed city centre would be presented as a real distance."""
        from tutor_match_meta.integrations.geo import BudgetedGeocoder, DisabledGeocoder

        geo = BudgetedGeocoder(DisabledGeocoder())
        assert await geo.resolve(pincode="999999", locality="Nowhere", city="Atlantis") is None

    def test_distance_is_arithmetic_not_a_service_call(self) -> None:
        from tutor_match_meta.contracts.tutor import GeoPoint
        from tutor_match_meta.integrations.geo import haversine_km

        gurugram = GeoPoint(latitude=28.4595, longitude=77.0266, granularity="city")
        delhi = GeoPoint(latitude=28.6139, longitude=77.2090, granularity="city")
        # ~24.7 km great-circle. Road distance is nearer 32; the proximity
        # evaluator is responsible for labelling this as straight-line, and
        # this test pins the arithmetic, not the travel time.
        assert 24.0 < haversine_km(gurugram, delhi) < 25.5
        assert haversine_km(gurugram, gurugram) == pytest.approx(0.0, abs=1e-9)

"""Model routing and the guarded provider wrapper.

Two jobs, both about keeping model spend and model risk out of the call sites.

**Routing (§15, §16).** A call site names a *purpose*, never a model. The
purpose → model mapping is configuration (`MODEL_EXTRACTION`,
`MODEL_ESCALATION`, `MODEL_EXPLANATION`, `MODEL_EMBEDDING`), so swapping a model
is an environment change with an evaluation run behind it, not a code edit. No
model id appears as a literal anywhere outside `config/settings.py`.

**Guarding.** `GuardedProvider` wraps any `LLMProvider` and enforces, in this
order, everything that must happen *before* money is spent:

    1. kill switch      an operator paused the model entirely
    2. call/token budget this conversation has spent its allowance
    3. rate limit        this conversation is being flooded
    4. the provider call

Ordering is the whole point. Every one of these was previously implemented and
none of them was on the path — `LimitScope.LLM` appeared only in a test file,
and `KillSwitches` was constructed by a factory nothing called. A limiter that
runs after the provider call is an audit log, not a limiter.

Usage is recorded for failures as well as successes, because the calls that
cost the most are usually the ones that timed out after three retries.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from tutor_match_meta.integrations.llm.provider import (
    LLMBudgetExceeded,
    LLMError,
    LLMPaused,
    LLMProvider,
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelTier,
    TokenBudget,
)
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter, estimate_cost_micros
from tutor_match_meta.prompts.registry import PromptCacheStats

logger = get_logger("llm.routing")


class Purpose(StrEnum):
    """Every reason this service may call a model. The list is the policy.

    Anything not on this list is deterministic code. Notably absent, and
    deliberately so (§15): date arithmetic, distance maths, sorting, SQL
    filtering, availability intersection, threshold checks and fee comparison.
    """

    EXTRACTION = "requirement_extraction"
    ESCALATION = "ambiguous_requirement_escalation"
    EXPLANATION = "shortlist_explanation"
    CLARIFICATION = "clarifying_question"
    EMBEDDING = "document_embedding"


#: Purpose → tier. Only `ESCALATION` may reach the reasoning tier, and even
#: then only when `TokenBudget.can_escalate()` allows it.
PURPOSE_TIER: dict[Purpose, ModelTier] = {
    Purpose.EXTRACTION: ModelTier.FAST,
    Purpose.ESCALATION: ModelTier.REASONING,
    Purpose.EXPLANATION: ModelTier.FAST,
    Purpose.CLARIFICATION: ModelTier.FAST,
    Purpose.EMBEDDING: ModelTier.FAST,
}


@dataclass(frozen=True, slots=True)
class ModelRouting:
    """Purpose → model id. Built from settings; no literals at call sites."""

    extraction: str
    escalation: str
    explanation: str
    embedding: str

    def model_for(self, purpose: Purpose) -> str:
        return {
            Purpose.EXTRACTION: self.extraction,
            Purpose.ESCALATION: self.escalation,
            Purpose.EXPLANATION: self.explanation,
            Purpose.CLARIFICATION: self.explanation,
            Purpose.EMBEDDING: self.embedding,
        }[purpose]

    def tier_for(self, purpose: Purpose) -> ModelTier:
        return PURPOSE_TIER[purpose]

    def as_dict(self) -> dict[str, str]:
        """For the operator health surface. Model ids are not secrets."""
        return {
            "extraction": self.extraction,
            "escalation": self.escalation,
            "explanation": self.explanation,
            "embedding": self.embedding,
        }


def routing_from_settings(settings: object) -> ModelRouting:
    def value(name: str, fallback: str) -> str:
        return str(getattr(settings, name, "") or fallback)

    tier1 = value("llm_tier1_model", "gpt-4o-mini")
    tier2 = value("llm_tier2_model", "gpt-4o")
    return ModelRouting(
        extraction=value("model_extraction", tier1),
        escalation=value("model_escalation", tier2),
        explanation=value("model_explanation", tier1),
        embedding=value("model_embedding", "text-embedding-3-small"),
    )


@dataclass(slots=True)
class UsageLedger:
    """Everything one conversation's model calls cost.

    Deliberately holds no prompt text, no completion text and no conversation
    id — only the pseudonymous ref. This object is what feeds cost metrics and
    the `llm_usage` table, and both are read by people who have no business
    seeing a parent's message (§16, §3).
    """

    conversation_ref: str = ""
    entries: list[LLMUsage] = field(default_factory=list)
    cost_micros: int = 0
    cache: PromptCacheStats = field(default_factory=PromptCacheStats)

    def record(self, usage: LLMUsage, *, prompt_ref: str) -> None:
        self.entries.append(usage)
        self.cost_micros += estimate_cost_micros(
            usage.model, usage.prompt_tokens, usage.completion_tokens
        )
        self.cache.record(
            prompt_ref=prompt_ref,
            input_tokens=usage.prompt_tokens,
            cached_tokens=usage.cached_prompt_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return sum(entry.total_tokens for entry in self.entries)

    def emit(self, metrics: MetricsEmitter) -> None:
        if not self.entries:
            return
        metrics.count(Metric.LLM_CALLS, len(self.entries))
        metrics.put(Metric.LLM_TOKENS, self.total_tokens)
        metrics.put(Metric.LLM_COST_MICROS, self.cost_micros)
        metrics.put(Metric.PROMPT_CACHE_HIT_RATE, self.cache.hit_rate * 100)
        slowest = max((e.latency_ms for e in self.entries), default=0)
        metrics.timing(Metric.LLM_LATENCY, slowest)


#: `async (scope_key) -> bool` — True when the call is within its rate limit.
RateCheck = Callable[[str], Awaitable[bool]]
#: `async () -> bool` — True when an operator has paused model calls.
PauseCheck = Callable[[], Awaitable[bool]]


class GuardedProvider:
    """Kill switch, budget and rate limit — all *before* the provider call."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        routing: ModelRouting,
        budget: TokenBudget,
        ledger: UsageLedger,
        rate_check: RateCheck | None = None,
        pause_check: PauseCheck | None = None,
        rate_scope_key: str = "",
    ) -> None:
        self._inner = inner
        self._routing = routing
        self._budget = budget
        self._ledger = ledger
        self._rate_check = rate_check
        self._pause_check = pause_check
        self._rate_scope_key = rate_scope_key

    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "unknown")

    @property
    def ledger(self) -> UsageLedger:
        return self._ledger

    async def structured(self, request: LLMRequest) -> LLMResponse:
        purpose = _purpose_of(request)
        tier = self._routing.tier_for(purpose) if purpose else request.tier

        # 1. Kill switch. LLM_PAUSED degrades to the deterministic path, so the
        #    caller catches this exactly like any other LLMError.
        if self._pause_check is not None and await self._pause_check():
            logger.info("llm call skipped: paused", extra={"tmm_purpose": request.purpose})
            raise LLMPaused("LLM_PAUSED kill switch is active")

        # 2. Budget: calls per turn, calls per conversation, escalation count.
        self._budget.assert_call_allowed(tier=tier)

        # 3. Rate limit. Checked before the spend so refused traffic is free —
        #    the difference between shedding an abusive conversation and paying
        #    OpenAI to serve it.
        if self._rate_check is not None and not await self._rate_check(self._rate_scope_key):
            raise LLMRateLimited("conversation LLM rate limit reached")

        routed = request if purpose is None else _with_tier(request, tier)
        try:
            response = await self._inner.structured(routed)
        except LLMError as exc:
            # Failures are recorded too: a call that timed out after retries
            # still consumed provider capacity and still cost latency.
            self._ledger.record(
                LLMUsage(
                    provider=self.name,
                    model=self._routing.model_for(purpose) if purpose else "unrouted",
                    tier=tier,
                    error_code=type(exc).__name__,
                ),
                prompt_ref=request.prompt_version,
            )
            raise

        self._budget.record(response.usage)
        self._ledger.record(response.usage, prompt_ref=request.prompt_version)
        return response


def _purpose_of(request: LLMRequest) -> Purpose | None:
    try:
        return Purpose(request.purpose)
    except ValueError:
        return None


def _with_tier(request: LLMRequest, tier: ModelTier) -> LLMRequest:
    if request.tier is tier:
        return request
    from dataclasses import replace

    return replace(request, tier=tier)


__all__ = [
    "PURPOSE_TIER",
    "GuardedProvider",
    "LLMBudgetExceeded",
    "ModelRouting",
    "Purpose",
    "UsageLedger",
    "routing_from_settings",
]

"""Provider-neutral LLM interface.

Domain code never imports `openai`. It depends on `LLMProvider`, which takes a
strict JSON schema and returns validated structured output or raises. That keeps
three promises:

* a model swap is a config change, not a code change;
* every call is budgeted, timed out, retried and metered by the same wrapper;
* no free-text model output ever reaches a decision — only schema-validated data.

Tiering (docs/assumptions.md A18): Tier 0 is no model at all. Tier 1 is a small
model for extraction and phrasing. Tier 2 is reserved for genuinely ambiguous
cases and requires an explicit trigger — "more intelligence sounds better" is not
a trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable


class ModelTier(IntEnum):
    """Escalation ladder. Higher costs more and must be justified."""

    DETERMINISTIC = 0
    FAST = 1
    REASONING = 2


class LLMError(Exception):
    """Base for every provider failure. Callers must always have a fallback."""


class LLMTimeout(LLMError):
    """The provider did not answer inside the configured budget."""


class LLMRateLimited(LLMError):
    """429 from the provider. Retryable with backoff."""


class LLMBudgetExceeded(LLMError):
    """This conversation has spent its token allowance. Not retryable."""


class LLMSchemaViolation(LLMError):
    """The model returned something that does not satisfy the schema."""


class LLMCircuitOpen(LLMError):
    """Too many recent failures; calls are being shed to protect latency."""


@dataclass(frozen=True, slots=True)
class LLMUsage:
    """What one call cost. Recorded for every call, including failures."""

    provider: str
    model: str
    tier: ModelTier
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: Input tokens the provider served from its own prompt-prefix cache, where
    #: it reports them. Tracked for the cost graph and the `prompt_cache_hit_rate`
    #: metric (§17); never relied on for correctness.
    cached_prompt_tokens: int = 0
    latency_ms: int = 0
    retry_count: int = 0
    cached: bool = False
    error_code: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def uncached_prompt_tokens(self) -> int:
        """What was actually billed at the full input rate."""
        return max(0, self.prompt_tokens - self.cached_prompt_tokens)


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One structured-output call.

    `schema` is required. There is no free-text completion method on this
    interface by design — if a caller wants prose, it asks for a schema with a
    string field, so the output is still bounded and validatable.
    """

    purpose: str
    system_prompt: str
    user_content: str
    schema: dict[str, Any]
    schema_name: str
    tier: ModelTier = ModelTier.FAST
    max_output_tokens: int | None = None
    temperature: float = 0.0
    #: Prompt template version, recorded with usage so a quality regression can
    #: be traced to a prompt change.
    prompt_version: str = "v1"


@dataclass(frozen=True, slots=True)
class LLMResponse:
    data: dict[str, Any]
    usage: LLMUsage


@runtime_checkable
class LLMProvider(Protocol):
    """The only LLM surface the rest of the service knows about."""

    name: str

    async def structured(self, request: LLMRequest) -> LLMResponse:
        """Return schema-valid data, or raise an `LLMError` subclass."""
        ...


class LLMPaused(LLMError):
    """An operator paused model calls. Deterministic path only, no retry."""


@dataclass
class TokenBudget:
    """Per-conversation spend cap, in three independent dimensions.

    Tokens alone are not enough. The realistic ways this service burns money
    are all *call-count* problems, not token-size problems:

    * a redelivery loop re-extracting the same message every few seconds;
    * a turn that escalates to the reasoning tier and then retries;
    * a conversation that never completes and re-asks on every inbound.

    So the budget caps tokens, calls per turn, calls per conversation, and
    escalations to the expensive tier — each with its own ceiling, because they
    fail in different ways and one generous limit hides the other three.

    Exhaustion is never an error the parent sees. Callers catch
    `LLMBudgetExceeded` and fall back to the deterministic path (§14).
    """

    limit: int
    #: Hard ceiling on model calls within one inbound message.
    max_calls_per_turn: int = 2
    #: Hard ceiling across the whole conversation's lifetime.
    max_calls_per_conversation: int = 12
    #: How many times this conversation may reach for the reasoning tier.
    max_escalations: int = 1

    spent: int = 0
    calls: int = 0
    turn_calls: int = 0
    escalations: int = 0
    by_tier: dict[ModelTier, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    @property
    def exhausted(self) -> bool:
        return (
            self.remaining <= 0
            or self.calls >= self.max_calls_per_conversation
            or self.turn_calls >= self.max_calls_per_turn
        )

    def begin_turn(self) -> None:
        """Reset the per-turn counter. Called once per inbound message."""
        self.turn_calls = 0

    def can_afford(self, estimated_tokens: int) -> bool:
        return self.spent + estimated_tokens <= self.limit

    def can_escalate(self) -> bool:
        return self.escalations < self.max_escalations

    def record(self, usage: LLMUsage) -> None:
        self.spent += usage.total_tokens
        self.calls += 1
        self.turn_calls += 1
        if usage.tier >= ModelTier.REASONING:
            self.escalations += 1
        self.by_tier[usage.tier] = self.by_tier.get(usage.tier, 0) + usage.total_tokens

    def assert_affordable(self, estimated_tokens: int) -> None:
        if not self.can_afford(estimated_tokens):
            raise LLMBudgetExceeded(
                f"conversation token budget exhausted: {self.spent}/{self.limit}"
            )

    def assert_call_allowed(self, *, tier: ModelTier = ModelTier.FAST) -> None:
        """Refuse before the call, not after. A cap checked afterwards is a log."""
        if self.turn_calls >= self.max_calls_per_turn:
            raise LLMBudgetExceeded(
                f"turn call budget exhausted: {self.turn_calls}/{self.max_calls_per_turn}"
            )
        if self.calls >= self.max_calls_per_conversation:
            raise LLMBudgetExceeded(
                f"conversation call budget exhausted: "
                f"{self.calls}/{self.max_calls_per_conversation}"
            )
        if tier >= ModelTier.REASONING and not self.can_escalate():
            raise LLMBudgetExceeded(
                f"escalation budget exhausted: {self.escalations}/{self.max_escalations}"
            )

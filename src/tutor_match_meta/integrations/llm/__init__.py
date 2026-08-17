"""Provider-neutral LLM access. Domain code never imports `openai`."""

from tutor_match_meta.integrations.llm.provider import (
    LLMBudgetExceeded,
    LLMCircuitOpen,
    LLMError,
    LLMProvider,
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMSchemaViolation,
    LLMTimeout,
    LLMUsage,
    ModelTier,
    TokenBudget,
)
from tutor_match_meta.integrations.llm.stub import DeterministicStubProvider

__all__ = [
    "DeterministicStubProvider",
    "LLMBudgetExceeded",
    "LLMCircuitOpen",
    "LLMError",
    "LLMProvider",
    "LLMRateLimited",
    "LLMRequest",
    "LLMResponse",
    "LLMSchemaViolation",
    "LLMTimeout",
    "LLMUsage",
    "ModelTier",
    "TokenBudget",
    "build_provider",
]


def build_provider(
    *,
    provider: str,
    api_key: str,
    tier1_model: str,
    tier2_model: str,
    base_url: str = "",
    timeout_seconds: float = 12.0,
    max_retries: int = 2,
    max_output_tokens: int = 800,
    store_responses: bool = False,
) -> LLMProvider:
    """Construct the configured provider. Falls back to the stub, never to None."""
    if provider != "openai" or not api_key:
        return DeterministicStubProvider()
    from tutor_match_meta.integrations.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(
        api_key=api_key,
        tier_models={ModelTier.FAST: tier1_model, ModelTier.REASONING: tier2_model},
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_output_tokens=max_output_tokens,
        store_responses=store_responses,
    )

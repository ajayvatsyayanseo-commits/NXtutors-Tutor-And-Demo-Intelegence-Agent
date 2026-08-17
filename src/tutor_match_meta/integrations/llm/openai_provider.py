"""OpenAI implementation of `LLMProvider`.

Everything risky about calling a model lives here and nowhere else: timeouts,
bounded retries with jittered backoff, a circuit breaker, strict JSON-schema
output, and usage telemetry emitted for failures as well as successes.

The API key is read once and never logged. Client construction is lazy so
importing this module costs nothing when `LLM_PROVIDER=stub`.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import TYPE_CHECKING, Any

from tutor_match_meta.integrations.llm.provider import (
    LLMCircuitOpen,
    LLMError,
    LLMRateLimited,
    LLMRequest,
    LLMResponse,
    LLMSchemaViolation,
    LLMTimeout,
    LLMUsage,
    ModelTier,
)
from tutor_match_meta.observability.context import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from openai import AsyncOpenAI

logger = get_logger("llm.openai")


class CircuitBreaker:
    """Sheds calls after repeated failures instead of queueing behind a dead API.

    Without this, an OpenAI outage turns every WhatsApp turn into a 12-second
    timeout and the SQS queue backs up. Open-circuit calls fail in microseconds
    and the deterministic path takes over.
    """

    def __init__(self, *, threshold: int = 5, reset_seconds: float = 30.0) -> None:
        self._threshold = threshold
        self._reset = reset_seconds
        self._failures = 0
        self._opened_at = 0.0

    @property
    def is_open(self) -> bool:
        if self._failures < self._threshold:
            return False
        if time.monotonic() - self._opened_at >= self._reset:
            # Half-open: let one probe through rather than reopening blindly.
            self._failures = self._threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = time.monotonic()


class OpenAIProvider:
    """Structured-output calls with retries, budget and telemetry."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        tier_models: dict[ModelTier, str],
        base_url: str = "",
        timeout_seconds: float = 12.0,
        max_retries: int = 2,
        max_output_tokens: int = 800,
        store_responses: bool = False,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAIProvider requires an API key")
        self._api_key = api_key
        self._base_url = base_url
        self._tier_models = tier_models
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._max_output_tokens = max_output_tokens
        self._store = store_responses
        self._circuit = circuit or CircuitBreaker()
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            from openai import AsyncOpenAI

            kwargs: dict[str, Any] = {"api_key": self._api_key, "timeout": self._timeout}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            # Retries are handled here so backoff and telemetry stay in one place.
            self._client = AsyncOpenAI(max_retries=0, **kwargs)
        return self._client

    def model_for(self, tier: ModelTier) -> str:
        return self._tier_models.get(tier) or self._tier_models[ModelTier.FAST]

    async def structured(self, request: LLMRequest) -> LLMResponse:
        if self._circuit.is_open:
            raise LLMCircuitOpen("openai circuit breaker is open")

        model = self.model_for(request.tier)
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                data, usage_raw = await self._call(request, model)
            except (LLMTimeout, LLMRateLimited) as exc:
                last_error = exc
                self._circuit.record_failure()
                if attempt < self._max_retries:
                    await asyncio.sleep(_backoff(attempt))
                    continue
            except LLMError as exc:
                # Deterministic failures (schema, auth, bad request) are not
                # retryable — retrying just burns the budget on the same error.
                self._circuit.record_failure()
                self._log_usage(request, model, started, attempt, error=type(exc).__name__)
                raise
            else:
                self._circuit.record_success()
                elapsed = int((time.perf_counter() - started) * 1000)
                return LLMResponse(
                    data=data,
                    usage=LLMUsage(
                        provider=self.name,
                        model=model,
                        tier=request.tier,
                        prompt_tokens=usage_raw.get("prompt_tokens", 0),
                        completion_tokens=usage_raw.get("completion_tokens", 0),
                        cached_prompt_tokens=usage_raw.get("cached_prompt_tokens", 0),
                        latency_ms=elapsed,
                        retry_count=attempt,
                    ),
                )

        self._log_usage(
            request,
            model,
            started,
            self._max_retries,
            error=type(last_error).__name__ if last_error else "unknown",
        )
        raise last_error or LLMError("openai call failed")

    async def _call(self, request: LLMRequest, model: str) -> tuple[dict[str, Any], dict[str, int]]:
        from openai import (
            APIConnectionError,
            APIStatusError,
            APITimeoutError,
            RateLimitError,
        )

        client = self._get_client()
        try:
            completion = await client.chat.completions.create(
                model=model,
                temperature=request.temperature,
                max_tokens=request.max_output_tokens or self._max_output_tokens,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    {"role": "user", "content": request.user_content},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "strict": True,
                        "schema": request.schema,
                    },
                },
                # Provider-side retention is configurable and off by default:
                # parent messages should not accumulate outside our control.
                store=self._store,
            )
        except APITimeoutError as exc:
            raise LLMTimeout(str(exc)) from exc
        except RateLimitError as exc:
            raise LLMRateLimited(str(exc)) from exc
        except APIConnectionError as exc:
            raise LLMTimeout(f"connection error: {exc}") from exc
        except APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMTimeout(f"upstream {exc.status_code}") from exc
            raise LLMError(f"openai {exc.status_code}") from exc

        choice = completion.choices[0]
        if choice.finish_reason == "length":
            # Truncated JSON is invalid JSON. Surfacing it as a schema violation
            # is more honest than parsing a half-object.
            raise LLMSchemaViolation("response truncated by max_tokens")

        content = choice.message.content or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMSchemaViolation(f"response was not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMSchemaViolation("response JSON was not an object")

        usage = completion.usage
        # `cached_tokens` lives on a nested details object that older API
        # versions omit entirely, so every hop is defensive. A missing value
        # means "no cache reporting", which reads as a 0% hit rate — the honest
        # answer, and never an error.
        details = getattr(usage, "prompt_tokens_details", None)
        return parsed, {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "cached_prompt_tokens": getattr(details, "cached_tokens", 0) or 0,
        }

    def _log_usage(
        self, request: LLMRequest, model: str, started: float, retries: int, *, error: str
    ) -> None:
        logger.warning(
            "llm call failed",
            extra={
                "tmm_model": model,
                "tmm_tier": int(request.tier),
                "tmm_purpose": request.purpose,
                "tmm_prompt_version": request.prompt_version,
                "tmm_retry_count": retries,
                "tmm_error_code": error,
                "tmm_latency_ms": int((time.perf_counter() - started) * 1000),
            },
        )


def _backoff(attempt: int, *, base_ms: int = 250) -> float:
    """Exponential backoff with full jitter."""
    ceiling = base_ms * (2**attempt) / 1000.0
    return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography

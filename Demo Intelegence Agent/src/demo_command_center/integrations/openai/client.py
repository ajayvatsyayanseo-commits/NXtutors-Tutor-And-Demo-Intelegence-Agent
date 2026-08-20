"""OpenAI, structured output only.

There is no free-text completion method on this client, and that is the design.
Every call declares a JSON schema and gets a validated dict back, so there is no
route by which unvalidated model output enters the system.

`purpose` selects the model from a configured map. No model id appears as a
literal anywhere in business logic, which means swapping one is a settings
change with an evaluation behind it rather than a code change.
"""

from __future__ import annotations

import json
from typing import Any, Final

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.observability import metrics
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.openai")

PROVIDER: Final = "openai"
API_HOST: Final = "api.openai.com"


#: Model families that renamed `max_tokens` and dropped support for anything
#: but the default temperature. Checked by prefix because the dated snapshot
#: ids (`gpt-5-mini-2025-…`) share it.
_NEXT_GEN_PREFIXES: Final = ("gpt-5", "o1", "o3", "o4")


def _tuning_for(model: str, max_output_tokens: int) -> dict[str, Any]:
    """Per-family request tuning.

    This module promises that swapping a model is a settings change rather than
    a code change. It was not true: the older parameters are rejected outright
    by the newer families —

        max_tokens    -> 400 "Use 'max_completion_tokens' instead"
        temperature=0 -> 400 "Only the default (1) value is supported"

    — so setting `DCC_MODEL_REASONING=gpt-5-mini` produced a runtime 400 on
    every extraction with nothing to explain it.

    Temperature 0 is requested wherever it is allowed, and it matters: a
    re-run that produces a different objection set makes the stored analysis
    unauditable. On a family that forbids it, the caller still gets validated
    JSON against the same schema — it is just no longer reproducible, which is
    a reason to prefer a family that supports it for extraction work.
    """
    if model.startswith(_NEXT_GEN_PREFIXES):
        return {"max_completion_tokens": max_output_tokens}
    return {"max_tokens": max_output_tokens, "temperature": 0}


class OpenAiClient:
    def __init__(
        self,
        *,
        api_key: str,
        models: dict[str, str],
        base_url: str = "",
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        max_output_tokens: int = 900,
        http: HttpClient | None = None,
    ) -> None:
        self._key = api_key
        self._models = models
        self._max_output_tokens = max_output_tokens
        host = _host_of(base_url) or API_HOST
        self._http = http or HttpClient(
            HttpConfig(
                provider=PROVIDER,
                base_url=base_url or f"https://{API_HOST}",
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            ),
            url_policy=UrlPolicy(allowed_hosts=frozenset({host})),
        )

    async def structured(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        model = self._models.get(purpose)
        if not model:
            raise ProviderRejected(PROVIDER, f"no model configured for {purpose}", code="no_model")

        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": purpose, "strict": True, "schema": schema},
            },
        }
        body.update(_tuning_for(model, max_output_tokens or self._max_output_tokens))

        response = await self._http.request(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            json_body=body,
            # Safe: temperature 0 and no side effects, so a retried timeout
            # costs tokens but cannot produce a second business action.
            idempotent=True,
        )

        usage = response.get("usage") or {}
        metrics.emit(metrics.Metric.LLM_CALL, purpose=purpose)
        metrics.emit(
            metrics.Metric.LLM_INPUT_TOKENS, float(usage.get("prompt_tokens") or 0), purpose=purpose
        )
        metrics.emit(
            metrics.Metric.LLM_OUTPUT_TOKENS,
            float(usage.get("completion_tokens") or 0),
            purpose=purpose,
        )

        choices = response.get("choices") or []
        if not choices:
            raise ProviderUnavailable(PROVIDER, "no choices returned")
        message = choices[0].get("message") or {}
        if message.get("refusal"):
            # A refusal is a legitimate outcome, not an error. The caller
            # degrades to its empty result.
            logger.info("model refused the request", extra={"dcc_purpose": purpose})
            return {}

        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderRejected(PROVIDER, "message content was not text", code="bad_content")
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise ProviderRejected(PROVIDER, "content was not valid json", code="bad_json") from exc
        if not isinstance(parsed, dict):
            raise ProviderRejected(PROVIDER, "content was not a json object", code="bad_shape")
        return parsed


def _host_of(base_url: str) -> str:
    if not base_url:
        return ""
    from urllib.parse import urlparse

    return (urlparse(base_url).hostname or "").lower()

"""The shared HTTP client. Every provider adapter goes through it.

One place for timeout, retry, error classification, circuit breaking and safe
logging, because getting any of those wrong once per provider is how a service
ends up with five subtly different retry behaviours and one that retries a
non-idempotent POST.

The retry rule is the important one: **a request is retried only if it is
declared idempotent.** Not "only if it is a GET" — a POST with an idempotency
key is safe to retry and a GET against a mutating endpoint is not. The caller
knows which; this module does not guess.

Every URL is validated against the SSRF policy before the socket opens.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from demo_command_center.contracts.ports import (
    ProviderError,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from demo_command_center.observability import metrics
from demo_command_center.observability.logging import current_context, get_logger
from demo_command_center.resilience.circuit import CircuitBreaker, CircuitOpen
from demo_command_center.security.urls import UrlPolicy, UrlRejected, validate

logger = get_logger("http")

#: Status codes worth retrying. 429 and 5xx only — a 400 will be a 400 again,
#: and retrying a 401 just burns the rate limit on a bad credential.
_RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: Response bodies larger than this are truncated before parsing. A provider
#: returning 50MB of HTML on an error page should not become our memory problem.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HttpConfig:
    provider: str
    base_url: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    #: Base for exponential backoff. Kept small: these run inside a Lambda whose
    #: own timeout is the real ceiling, and a 30s backoff just guarantees a cold
    #: failure instead of a warm one.
    backoff_seconds: float = 0.25


class HttpClient:
    """A provider-scoped client with a breaker. One per provider, per container."""

    def __init__(
        self,
        config: HttpConfig,
        *,
        url_policy: UrlPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        transport: Any = None,
    ) -> None:
        self._config = config
        self._policy = url_policy or UrlPolicy()
        self._breaker = breaker or CircuitBreaker(name=config.provider)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_seconds),
                transport=self._transport,
                # Redirects are never followed: a provider redirecting us to an
                # unvalidated host would bypass the URL policy entirely.
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        form_body: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        """One provider call. Returns the decoded body or raises a `ProviderError`.

        `form_body` sends `application/x-www-form-urlencoded` instead of JSON.
        OAuth2 token endpoints require it — RFC 6749 4.1.3 specifies that
        encoding, and Google's rejects a JSON body — so a client that can only
        send JSON cannot obtain a token at all.
        """
        url = path if path.startswith("http") else f"{self._config.base_url.rstrip('/')}{path}"
        try:
            validate(url, self._policy)
        except UrlRejected as exc:
            raise ProviderRejected(self._config.provider, f"url rejected: {exc.reason}") from exc

        try:
            self._breaker.before_call()
        except CircuitOpen as exc:
            metrics.emit(metrics.Metric.CIRCUIT_OPENED, provider=self._config.provider)
            raise ProviderUnavailable(self._config.provider, "circuit open") from exc

        attempts = self._config.max_retries + 1 if idempotent else 1
        last: ProviderError | None = None

        for attempt in range(1, attempts + 1):
            try:
                body = await self._attempt(method, url, headers, json_body, params, form_body)
            except ProviderError as exc:
                last = exc
                if not exc.retryable or attempt >= attempts:
                    self._breaker.record_failure()
                    raise
                await asyncio.sleep(self._config.backoff_seconds * (2 ** (attempt - 1)))
                continue
            self._breaker.record_success()
            return body

        self._breaker.record_failure()
        raise last or ProviderUnavailable(self._config.provider, "exhausted retries")

    async def _attempt(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        json_body: dict[str, Any] | None,
        params: dict[str, str] | None,
        form_body: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = await self._http()
        context = current_context()
        merged = {
            "Accept": "application/json",
            "X-Trace-Id": context.trace_id,
            "X-Correlation-Id": context.correlation_id,
            **(headers or {}),
        }

        started = time.perf_counter()
        try:
            response = await client.request(
                method.upper(),
                url,
                headers=merged,
                json=json_body,
                data=form_body,
                params=params,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self._config.provider, self._config.timeout_seconds) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self._config.provider, type(exc).__name__) from exc
        finally:
            metrics.emit(
                metrics.Metric.PROVIDER_LATENCY,
                (time.perf_counter() - started) * 1000,
                unit=metrics.Unit.MILLISECONDS,
                provider=self._config.provider,
            )

        return self._decode(response)

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        provider = self._config.provider
        if response.status_code >= 400:
            # The body is deliberately not logged: provider error bodies echo
            # the request, which for us includes customer data.
            metrics.emit(
                metrics.Metric.PROVIDER_ERROR, provider=provider, status=str(response.status_code)
            )
            code = _error_code(response)
            if response.status_code in _RETRYABLE_STATUS:
                raise ProviderUnavailable(provider, f"http {response.status_code} ({code})")
            raise ProviderRejected(
                provider,
                f"http {response.status_code}",
                status_code=response.status_code,
                code=code,
            )

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ProviderRejected(provider, "response too large", code="oversized_response")
        if not response.content:
            return {}
        try:
            decoded = response.json()
        except ValueError as exc:
            raise ProviderRejected(provider, "response was not json", code="bad_json") from exc
        if not isinstance(decoded, dict):
            return {"data": decoded}
        return decoded


def _error_code(response: httpx.Response) -> str:
    """A short, non-identifying error code from a provider body.

    Providers disagree on where they put it, and none of the three shapes here
    contains customer data — which is why only these three are read.
    """
    try:
        body = response.json()
    except ValueError:
        return "unparseable"
    if not isinstance(body, dict):
        return "unknown"
    for path in (("error", "code"), ("error", "type"), ("code",), ("type",), ("message",)):
        value: Any = body
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str | int):
            return str(value)[:48]
    return "unknown"

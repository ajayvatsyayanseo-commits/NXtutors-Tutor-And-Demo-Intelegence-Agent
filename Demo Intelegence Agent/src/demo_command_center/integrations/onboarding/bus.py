"""Agent-to-agent dispatch — the handoff transport.

Signed with the same HMAC scheme as the gateway client, over method, path,
timestamp and body hash. Binding the path into the signature is what stops a
valid handoff envelope being replayed against a different endpoint.

Retries are permitted because every envelope carries an `idempotency_key` and
the receiving agent is required to dedupe on it. Without that guarantee a
retried handoff creates two onboarding records for one customer, so the key is
asserted here rather than assumed.
"""

from __future__ import annotations

import json
import time
from typing import Any, Final
from urllib.parse import urlparse

from demo_command_center.contracts.ports import ProviderRejected
from demo_command_center.observability.logging import get_logger
from demo_command_center.resilience.http import HttpClient, HttpConfig
from demo_command_center.security.signatures import SignedRequest, sign_internal
from demo_command_center.security.urls import UrlPolicy

logger = get_logger("integration.agents")

PROVIDER: Final = "agent_bus"


class HttpAgentBus:
    def __init__(
        self,
        *,
        signing_secret: str,
        timeout_seconds: float = 8.0,
        max_retries: int = 2,
        http: HttpClient | None = None,
    ) -> None:
        self._secret = signing_secret
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._http = http
        self._clients: dict[str, HttpClient] = {}

    async def dispatch(self, envelope: dict[str, Any], *, url: str) -> dict[str, Any]:
        if not url:
            raise ProviderRejected(PROVIDER, "no destination url configured", code="no_url")
        if not envelope.get("idempotency_key"):
            # A handoff the receiver cannot dedupe must not be retryable, and we
            # would rather fail loudly than send a retryable duplicate.
            raise ProviderRejected(
                PROVIDER, "envelope has no idempotency_key", code="no_idempotency_key"
            )

        parsed = urlparse(url)
        path = parsed.path or "/"
        raw = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
        timestamp = int(time.time())
        signature = sign_internal(
            self._secret, SignedRequest(method="POST", path=path, timestamp=timestamp, body=raw)
        )

        client = self._client_for(url)
        return await client.request(
            "POST",
            url,
            headers={
                "Content-Type": "application/json",
                "X-Nxt-Signature": signature,
                "X-Nxt-Timestamp": str(timestamp),
                "X-Idempotency-Key": str(envelope["idempotency_key"]),
                "X-Nxt-Agent": str(envelope.get("source_agent") or ""),
            },
            json_body=envelope,
            idempotent=True,
        )

    def _client_for(self, url: str) -> HttpClient:
        """One client, and one circuit breaker, per destination host.

        Sharing a breaker across peers would let a broken onboarding agent trip
        the circuit for every other handoff destination.
        """
        if self._http is not None:
            return self._http
        host = (urlparse(url).hostname or "").lower()
        if host not in self._clients:
            self._clients[host] = HttpClient(
                HttpConfig(
                    provider=f"{PROVIDER}:{host}",
                    base_url=f"{urlparse(url).scheme}://{host}",
                    timeout_seconds=self._timeout,
                    max_retries=self._max_retries,
                ),
                url_policy=UrlPolicy(allowed_hosts=frozenset({host})),
            )
        return self._clients[host]

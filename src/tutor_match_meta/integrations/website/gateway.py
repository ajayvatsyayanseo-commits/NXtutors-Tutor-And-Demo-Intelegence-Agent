"""Write-back transport.

`WebsiteCommandGateway` is the only way a command reaches the website. Three
implementations, deliberately ordered by how much we trust them:

* `LaravelApiGateway` — signed HTTPS to the site's own service layer. Preferred:
  Laravel applies its business rules, and the agent needs no database grant.
* `RecordingGateway` — local/test. Records and acknowledges.
* `DisabledGateway` — the default when write-back is off. Refuses loudly rather
  than silently dropping, so a misconfiguration surfaces immediately.

There is no direct-database writer. The agent holds no grant on the website's
store, so a bug here cannot corrupt the website's data — the worst it can do
is send a command the website's own validation rejects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from tutor_match_meta.integrations.website.commands import CommandEnvelope, WebsiteCommand
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.security.signing import SignedRequest, sign
from tutor_match_meta.security.urls import UrlPolicy, validate

logger = get_logger("website.gateway")

COMMAND_PATH = "/internal/agent/commands"


@dataclass(frozen=True, slots=True)
class CommandResult:
    accepted: bool
    #: True when the website reports it had already applied this idempotency key.
    duplicate: bool = False
    status_code: int | None = None
    reason: str = ""
    reference: str | None = None

    @property
    def settled(self) -> bool:
        """Accepted or already-applied: either way, stop retrying."""
        return self.accepted or self.duplicate


@runtime_checkable
class WebsiteCommandGateway(Protocol):
    async def execute(self, command: WebsiteCommand, *, trace_id: str) -> CommandResult: ...


class DisabledGateway:
    """Write-back off. Refuses explicitly — silence would hide a misconfig."""

    def __init__(self, reason: str = "website_write_disabled") -> None:
        self._reason = reason

    async def execute(self, command: WebsiteCommand, *, trace_id: str) -> CommandResult:
        logger.info(
            "website command suppressed",
            extra={"tmm_command": command.name, "tmm_reason": self._reason},
        )
        return CommandResult(accepted=False, reason=self._reason)


class RecordingGateway:
    """Local and test transport. Records envelopes and acknowledges once each."""

    def __init__(self) -> None:
        self.executed: list[CommandEnvelope] = []
        self._seen: set[str] = set()

    async def execute(self, command: WebsiteCommand, *, trace_id: str) -> CommandResult:
        envelope = command.envelope(trace_id=trace_id)
        if envelope.idempotency_key in self._seen:
            return CommandResult(accepted=False, duplicate=True, reason="already_applied")
        self._seen.add(envelope.idempotency_key)
        self.executed.append(envelope)
        return CommandResult(accepted=True, reference=envelope.idempotency_key)


class LaravelApiGateway:
    """Signed HTTPS to the website's internal integration endpoint.

    Retries only transient failures. A 4xx is a contract problem — retrying it
    would burn the retry budget and delay the DLQ signal that something is
    genuinely wrong.
    """

    def __init__(
        self,
        *,
        base_url: str,
        signing_key: str,
        url_policy: UrlPolicy,
        timeout_seconds: float = 8.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not signing_key:
            raise ValueError("LaravelApiGateway requires a base_url and signing_key")
        self._base_url = base_url.rstrip("/")
        self._signing_key = signing_key
        self._url_policy = url_policy
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self.failures = 0

    async def execute(self, command: WebsiteCommand, *, trace_id: str) -> CommandResult:
        envelope = command.envelope(trace_id=trace_id)
        body = envelope.to_json().encode("utf-8")
        url = validate(f"{self._base_url}{COMMAND_PATH}", self._url_policy)

        last_status: int | None = None
        for attempt in range(self._max_retries + 1):
            timestamp = int(time.time())
            signature = sign(
                self._signing_key,
                SignedRequest("POST", COMMAND_PATH, timestamp, body),
            )
            headers = {
                "Content-Type": "application/json",
                "X-Nxt-Signature": signature,
                "X-Nxt-Timestamp": str(timestamp),
                "X-Nxt-Agent": envelope.source_agent,
                "X-Trace-Id": trace_id,
                "X-Idempotency-Key": envelope.idempotency_key,
            }
            try:
                response = await self._client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                self.failures += 1
                if attempt < self._max_retries:
                    continue
                return CommandResult(accepted=False, reason=f"transport_error:{type(exc).__name__}")

            last_status = response.status_code
            if response.status_code == 409:
                # The website already applied this key. Success, not a failure.
                return CommandResult(
                    accepted=False, duplicate=True, status_code=409, reason="already_applied"
                )
            if response.status_code < 300:
                return CommandResult(
                    accepted=True,
                    status_code=response.status_code,
                    reference=_reference(response),
                )
            if 400 <= response.status_code < 500:
                self.failures += 1
                logger.error(
                    "website rejected command",
                    extra={"tmm_command": command.name, "tmm_status": response.status_code},
                )
                return CommandResult(
                    accepted=False,
                    status_code=response.status_code,
                    reason="rejected",
                )
            self.failures += 1  # 5xx: retry

        return CommandResult(accepted=False, status_code=last_status, reason="upstream_unavailable")

    async def close(self) -> None:
        await self._client.aclose()


def _reference(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return str(body.get("reference")) if isinstance(body, dict) and body.get("reference") else None


def build_gateway(
    *,
    enabled: bool,
    base_url: str,
    signing_key: str,
    url_policy: UrlPolicy,
) -> WebsiteCommandGateway:
    if not enabled:
        return DisabledGateway()
    if not base_url or not signing_key:
        return DisabledGateway("website_api_not_configured")
    return LaravelApiGateway(base_url=base_url, signing_key=signing_key, url_policy=url_policy)

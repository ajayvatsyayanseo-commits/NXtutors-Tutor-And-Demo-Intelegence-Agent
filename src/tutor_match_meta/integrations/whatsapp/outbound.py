"""Outbound WhatsApp delivery.

Kept strictly separate from the decision path. The outbound worker consumes its
own queue, so a Meta API outage retries *delivery* without ever re-running
matching — regenerating a shortlist because a message failed to send would show
the parent a different set of tutors on the retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.security.urls import UrlPolicy, validate

logger = get_logger("whatsapp.outbound")

#: Meta's hard limit for a text body.
MAX_BODY_CHARS = 4_096
META_HOST = "graph.facebook.com"


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    #: E.164 recipient. Present only in the outbound queue, never in logs.
    to: str
    body: str
    #: Provider-side idempotency: the same key must not send twice.
    dedup_key: str
    trace_id: str
    preview_url: bool = True


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivered: bool
    provider_message_id: str | None = None
    status_code: int | None = None
    reason: str = ""
    retryable: bool = False


@runtime_checkable
class WhatsAppSender(Protocol):
    async def send(self, message: OutboundMessage) -> DeliveryResult: ...


class RecordingSender:
    """Local and test transport. Keeps what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self._keys: set[str] = set()

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if message.dedup_key in self._keys:
            return DeliveryResult(delivered=False, reason="duplicate_suppressed")
        self._keys.add(message.dedup_key)
        self.sent.append(message)
        return DeliveryResult(delivered=True, provider_message_id=f"local-{len(self.sent)}")


class LoggingSender:
    """Writes the message to the log instead of sending. For dev environments."""

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        logger.info(
            "whatsapp message (not sent)",
            extra={"tmm_body_chars": len(message.body), "tmm_dedup_key": message.dedup_key},
        )
        return DeliveryResult(delivered=True, provider_message_id="logged")


class MetaCloudSender:
    """Meta Cloud API transport.

    Distinguishes retryable from permanent failures precisely, because retrying a
    permanent one (invalid recipient, 24-hour window closed) wastes the DLQ
    signal and delays a human noticing.
    """

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        api_version: str = "v21.0",
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
        url_policy: UrlPolicy | None = None,
    ) -> None:
        if not phone_number_id or not access_token:
            raise ValueError("MetaCloudSender requires phone_number_id and access_token")
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._version = api_version
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._url_policy = url_policy or UrlPolicy(allowed_hosts=frozenset({META_HOST}))
        self.failures = 0

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        url = validate(
            f"https://{META_HOST}/{self._version}/{self._phone_number_id}/messages",
            self._url_policy,
        )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _normalise_recipient(message.to),
            "type": "text",
            "text": {"preview_url": message.preview_url, "body": message.body[:MAX_BODY_CHARS]},
        }
        try:
            response = await self._client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            self.failures += 1
            return DeliveryResult(
                delivered=False, reason=f"transport:{type(exc).__name__}", retryable=True
            )

        if response.status_code < 300:
            return DeliveryResult(
                delivered=True,
                provider_message_id=_message_id(response),
                status_code=response.status_code,
            )

        self.failures += 1
        retryable = response.status_code == 429 or response.status_code >= 500
        logger.warning(
            "whatsapp delivery failed",
            extra={"tmm_status": response.status_code, "tmm_retryable": retryable},
        )
        return DeliveryResult(
            delivered=False,
            status_code=response.status_code,
            reason=_error_code(response),
            retryable=retryable,
        )

    async def close(self) -> None:
        await self._client.aclose()


def _normalise_recipient(raw: str) -> str:
    """Digits only, as the Cloud API expects."""
    return re.sub(r"\D", "", raw)


def _message_id(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    messages = body.get("messages") if isinstance(body, dict) else None
    if isinstance(messages, list) and messages:
        return str(messages[0].get("id"))
    return None


def _error_code(response: httpx.Response) -> str:
    """Meta's error code, never the full body — it can echo message content."""
    try:
        body = response.json()
    except ValueError:
        return f"http_{response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return f"meta_{error.get('code', 'unknown')}"
    return f"http_{response.status_code}"


@dataclass(slots=True)
class DeliveryLedger:
    """Tracks delivery attempts so retries stay bounded and observable."""

    max_attempts: int = 4
    attempts: dict[str, int] = field(default_factory=dict)

    def record(self, dedup_key: str) -> int:
        self.attempts[dedup_key] = self.attempts.get(dedup_key, 0) + 1
        return self.attempts[dedup_key]

    def exhausted(self, dedup_key: str) -> bool:
        return self.attempts.get(dedup_key, 0) >= self.max_attempts


def build_sender(
    *, enabled: bool, phone_number_id: str, access_token: str, api_version: str
) -> WhatsAppSender:
    if not enabled or not phone_number_id or not access_token:
        return LoggingSender()
    return MetaCloudSender(
        phone_number_id=phone_number_id, access_token=access_token, api_version=api_version
    )

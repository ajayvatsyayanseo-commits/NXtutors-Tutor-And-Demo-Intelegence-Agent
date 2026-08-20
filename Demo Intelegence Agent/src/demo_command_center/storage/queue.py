"""SQS publishing.

This did not exist, and its absence was the quietest possible production
failure. `handlers/webhooks._enqueue_meta` is named for enqueuing: it flattened
Meta's batch into work items, claimed an idempotency key for each, and then
returned the count. Nothing was ever put on a queue. A parent's WhatsApp
message was received, signature-verified, deduplicated — and dropped. The
webhook answered 200, every metric looked healthy, and the orchestrator never
saw a single turn.

Two properties matter here:

* **The lane is chosen by the caller, not guessed.** Each queue has its own
  visibility timeout, concurrency and dead-letter queue, and putting a payment
  event on the analytics lane silently gives it the wrong retry behaviour.
* **FIFO lanes are ordered per conversation.** `MessageGroupId` is the
  conversation ref, so two turns of one conversation cannot be processed out of
  order, while different conversations still run in parallel.

`boto3` is in the Lambda runtime already, so this adds no package weight.
"""

from __future__ import annotations

import json
from typing import Any, Final, Protocol, runtime_checkable

from demo_command_center.contracts.ports import ProviderUnavailable
from demo_command_center.observability.logging import get_logger
from demo_command_center.security.signatures import idempotency_key

logger = get_logger("storage.queue")

PROVIDER: Final = "sqs"

#: SQS rejects a body over 256 KiB. A work item is a few hundred bytes; this
#: only ever trips on a payload that should not have been inlined.
MAX_BODY_BYTES: Final = 262_144


@runtime_checkable
class QueuePublisherPort(Protocol):
    async def publish(
        self,
        *,
        queue_url: str,
        body: dict[str, Any],
        group_id: str = "",
        dedup_id: str = "",
    ) -> str: ...


class SqsPublisher:
    """Publishes work items to SQS. One boto3 client per container."""

    def __init__(self, client: Any = None, *, region: str = "") -> None:
        self._client = client
        self._region = region

    def _sqs(self) -> Any:
        if self._client is None:  # pragma: no cover - AWS-gated
            import boto3

            self._client = boto3.client("sqs", region_name=self._region or None)
        return self._client

    async def publish(
        self,
        *,
        queue_url: str,
        body: dict[str, Any],
        group_id: str = "",
        dedup_id: str = "",
    ) -> str:
        if not queue_url:
            # Refused rather than dropped. A missing queue URL is a deployment
            # error, and swallowing it here is exactly how the inbound path
            # came to be silently inert.
            raise ProviderUnavailable(PROVIDER, "no queue url configured for this lane")

        payload = json.dumps(body, default=str)
        if len(payload.encode("utf-8")) > MAX_BODY_BYTES:
            raise ProviderUnavailable(PROVIDER, "work item exceeds the SQS body limit")

        request: dict[str, Any] = {"QueueUrl": queue_url, "MessageBody": payload}
        if queue_url.endswith(".fifo"):
            # Both are required on a FIFO queue. The group orders one
            # conversation against itself; the dedup id is what makes an
            # at-least-once webhook redelivery a no-op inside the 5-minute
            # window rather than a duplicate turn.
            request["MessageGroupId"] = group_id or "default"
            request["MessageDeduplicationId"] = dedup_id or idempotency_key("sqs", payload)

        try:
            response = self._sqs().send_message(**request)
        except Exception as exc:  # pragma: no cover - AWS-gated
            raise ProviderUnavailable(PROVIDER, type(exc).__name__) from exc

        message_id = str(response.get("MessageId") or "")
        logger.info(
            "work item published",
            extra={"dcc_queue": queue_url.rsplit("/", 1)[-1], "dcc_message_id": message_id},
        )
        return message_id


class NullPublisher:
    """Records instead of publishing. Local runs and tests only.

    It logs at WARNING on every call: a publisher that quietly accepts work is
    indistinguishable from a working one, which is the failure this module
    exists to prevent.
    """

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(
        self,
        *,
        queue_url: str,
        body: dict[str, Any],
        group_id: str = "",
        dedup_id: str = "",
    ) -> str:
        self.published.append({"queue_url": queue_url, "body": body, "group_id": group_id})
        logger.warning(
            "queue publish skipped; no SQS configured",
            extra={"dcc_trigger": str(body.get("trigger") or "")},
        )
        return f"local-{len(self.published)}"


def build_publisher(settings: Any) -> QueuePublisherPort:
    """SQS when a work queue is configured, a recording null otherwise."""
    if settings.work_queue_url:
        return SqsPublisher(region=settings.aws_region)
    return NullPublisher()

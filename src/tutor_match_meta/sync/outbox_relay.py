"""Outbox relay — drains pending side effects to SQS.

The match worker enqueues into the outbox in the same transaction as the state
change. This job pushes those rows onto the outbound queue.

It is a safety net, not the primary path: the worker normally sends to SQS
directly. The relay exists for the case where the worker crashed between
committing the decision and sending the message, which is exactly the window a
transactional outbox is meant to close.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from tutor_match_meta.config.settings import Settings
from tutor_match_meta.contracts.outbound import OutboundDeliveryV1
from tutor_match_meta.observability.context import get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter
from tutor_match_meta.repositories.postgres import PostgresOutbox, build_sessions, create_engine

logger = get_logger("sync.outbox")

BATCH_SIZE = 25
#: Exponential, capped. A message that has failed five times is marked dead and
#: surfaces through the DLQ alarm instead of retrying forever.
BACKOFF_SECONDS = (30, 120, 600, 1800, 3600)
#: A relay Lambda cannot run longer than its 300s timeout, so a `claiming` row
#: older than this belongs to an invocation that died. Generous on purpose:
#: reclaiming too eagerly is how a message gets sent twice.
CLAIM_LEASE_SECONDS = 900


def _retry_at(attempts: int, now: datetime) -> datetime:
    index = min(attempts, len(BACKOFF_SECONDS) - 1)
    return now + timedelta(seconds=BACKOFF_SECONDS[index])


async def relay(settings: Settings) -> dict[str, Any]:
    """Drain claimed outbox rows to SQS.

    Ordering matters: reclaim abandoned leases *before* claiming, or a row
    orphaned by a crashed relay is never picked up again — it is neither
    `pending` (so no relay sees it) nor `dead` (so no alarm fires).
    """
    outbox = PostgresOutbox(build_sessions(create_engine(settings)))
    metrics = MetricsEmitter().with_dimensions(environment=settings.environment.value)
    now = datetime.now(UTC)

    reclaimed = await outbox.reclaim_stale(older_than=now - timedelta(seconds=CLAIM_LEASE_SECONDS))
    if reclaimed:
        logger.warning("reclaimed abandoned outbox leases", extra={"tmm_reclaimed": reclaimed})

    pending = await outbox.claim_batch(limit=BATCH_SIZE, now=now)
    metrics.put(Metric.OUTBOX_PENDING, len(pending))
    if not pending:
        metrics.flush()
        return {"relayed": 0, "reclaimed": reclaimed}

    if not settings.outbound_queue_url:
        # The rows are claimed. Release them so the next run can retry rather
        # than leaving them leased against a misconfiguration.
        for message in pending:
            await outbox.mark_failed(
                message.dedup_key, error="no_outbound_queue_configured", retry_at=now
            )
        logger.warning("outbox relay skipped: no outbound queue configured")
        metrics.flush()
        return {"skipped": "no_outbound_queue", "pending": len(pending), "reclaimed": reclaimed}

    import boto3

    client = boto3.client("sqs", region_name=settings.aws_region)
    relayed = 0
    failed = 0
    invalid = 0
    audited = 0

    for message in pending:
        try:
            delivery = _delivery(message)
        except ValidationError as exc:
            # An unaddressable row can never be delivered by any retry. Burn
            # the attempts immediately so it reaches `dead` and alarms, instead
            # of cycling through the backoff ladder for six hours first.
            invalid += 1
            logger.error(
                "outbox row is not a valid delivery; marking dead",
                extra={"tmm_kind": message.kind, "tmm_errors": exc.error_count()},
            )
            await outbox.mark_dead(message.dedup_key, error="invalid_delivery_payload")
            continue

        if not delivery.deliverable:
            # The caller already sent this text. The row is the audit record,
            # not work; close it so it does not sit in the backlog for ever and
            # trip the queue-age alarm.
            await outbox.mark_delivered(message.dedup_key)
            audited += 1
            continue

        try:
            client.send_message(
                QueueUrl=settings.outbound_queue_url, MessageBody=delivery.model_dump_json()
            )
        except Exception as exc:  # recorded and retried, never swallowed
            failed += 1
            await outbox.mark_failed(
                message.dedup_key,
                error=f"{type(exc).__name__}: {exc}",
                retry_at=_retry_at(message.attempts, now),
            )
            continue
        await outbox.mark_delivered(message.dedup_key)
        relayed += 1

    if invalid:
        metrics.count(Metric.OUTBOX_DEAD, invalid)
    if reclaimed:
        metrics.count(Metric.OUTBOX_RECLAIMED, reclaimed)
    metrics.flush()
    logger.info(
        "outbox relayed",
        extra={
            "tmm_relayed": relayed,
            "tmm_audited": audited,
            "tmm_failed": failed,
            "tmm_invalid": invalid,
        },
    )
    return {
        "relayed": relayed,
        "audited": audited,
        "failed": failed,
        "invalid": invalid,
        "reclaimed": reclaimed,
    }


def _delivery(message: Any) -> OutboundDeliveryV1:
    """Parse one outbox row against the shared delivery contract.

    Validating here rather than trusting the stored dict is what stops a row
    written by an older deployment from reaching a worker that cannot parse it.
    """
    return OutboundDeliveryV1.from_payload(
        {**dict(message.payload), "kind": message.kind, "trace_id": message.trace_id}
    )

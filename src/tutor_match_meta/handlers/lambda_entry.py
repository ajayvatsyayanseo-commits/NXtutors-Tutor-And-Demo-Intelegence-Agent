"""AWS Lambda entry points.

Five functions, one module. Each is a thin adapter: parse the AWS event shape,
delegate to a service, translate the result back. All the logic lives in the
services so it is testable without AWS.

Each one declares which side of the network boundary it runs on, and that is
not cosmetic: there is no NAT Gateway, so `match_worker` (VPC) cannot reach
the public internet and `enrich_worker` / `outbound_worker` (internet) cannot
reach PostgreSQL. See contracts/enrichment.py for the full topology.

**Partial batch failures matter.** Every SQS consumer returns
`batchItemFailures`, so one poison message does not force redelivery of the nine
that succeeded alongside it. Without that, a single malformed record can replay
an entire batch until it hits the DLQ.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from tutor_match_meta.config.settings import get_settings
from tutor_match_meta.contracts.inbound import InboundEnvelope
from tutor_match_meta.observability.context import (
    RequestContext,
    configure_logging,
    get_logger,
    new_trace_id,
    request_context,
)
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter

logger = get_logger("lambda")


def _run(coro: Any) -> Any:
    """Run an async service from Lambda's sync entry point."""
    return asyncio.run(coro)


# --------------------------------------------------------------------- ingress
def ingress_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """API Gateway (HTTP API v2) -> validate, enqueue, 202."""
    from tutor_match_meta.bootstrap import build_ingress_service

    settings = get_settings()
    configure_logging(settings.log_level)

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    normalised = {
        "X-Nxt-Signature": headers.get("x-nxt-signature", ""),
        "X-Nxt-Timestamp": headers.get("x-nxt-timestamp", ""),
        "X-Nxt-Agent": headers.get("x-nxt-agent", ""),
        "X-Trace-Id": headers.get("x-trace-id", ""),
    }
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(body)
    else:
        raw = body.encode("utf-8")

    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "POST")
    path = http.get("path", "/ingress")

    import time as clock

    service = build_ingress_service(settings)
    response = _run(
        service.handle(method=method, path=path, headers=normalised, body=raw, now=clock.time())
    )
    result: dict[str, Any] = response.to_api_gateway()
    return result


# ---------------------------------------------------------------- enrich worker
def enrich_worker_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """SQS FIFO -> resolve everything that needs the internet -> match queue.

    Runs **outside** the VPC. It calls OpenAI and the memory service, touches no
    database, and forwards the same envelope with an `EnrichmentV1` attached.

    Enrichment is best-effort by contract: if the model is unreachable, the
    envelope still goes forward carrying a deterministic parse and a `degraded`
    marker. The one thing that must never happen is dropping the turn, so a
    failure to *enrich* is not a batch-item failure — only a failure to
    *forward* is, because that would lose the parent's message.
    """
    from tutor_match_meta.bootstrap import (
        build_enrichment_service,
        build_match_enqueue,
        build_pseudonymiser,
    )

    settings = get_settings()
    configure_logging(settings.log_level)
    service = build_enrichment_service(settings)
    forward = build_match_enqueue(settings)
    pseudonymiser = build_pseudonymiser(settings)

    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            envelope = InboundEnvelope.model_validate_json(record["body"])
        except Exception:
            # Unparseable: retrying cannot fix it. Report it so it reaches the
            # DLQ where a human sees it, rather than deleting it silently.
            logger.exception("unparseable enrich record", extra={"tmm_message_id": message_id})
            failures.append({"itemIdentifier": message_id})
            continue

        metrics = MetricsEmitter().with_dimensions(
            environment=settings.environment.value, source=envelope.source_agent
        )
        with request_context(
            RequestContext(
                trace_id=envelope.trace_id,
                conversation_id_hash=pseudonymiser.conversation(envelope.conversation_id),
                message_id=message_id,
                source_agent=envelope.source_agent,
            )
        ):
            try:
                enriched = _run(service.enrich(envelope))
            except Exception:
                # Never lose the turn over an optional dependency.
                logger.exception("enrichment failed; forwarding unenriched")
                metrics.count(Metric.DEGRADED_TURN)
                enriched = envelope

            try:
                _run(forward(enriched))
            except Exception:
                logger.exception("could not forward to the match queue")
                failures.append({"itemIdentifier": message_id})
            finally:
                metrics.flush()

    return {"batchItemFailures": failures}


# ----------------------------------------------------------------- match worker
def match_worker_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """SQS FIFO -> one full matching turn per record."""
    from tutor_match_meta.bootstrap import build_turn_service

    settings = get_settings()
    configure_logging(settings.log_level)
    service, pseudonymiser = _run(build_turn_service(settings))

    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            envelope = InboundEnvelope.model_validate_json(record["body"])
        except Exception:
            # Not retryable: send it straight to the DLQ rather than looping.
            logger.exception("unparseable sqs record", extra={"tmm_message_id": message_id})
            continue

        metrics = MetricsEmitter().with_dimensions(
            environment=settings.environment.value, source=envelope.source_agent
        )
        ctx = RequestContext(
            trace_id=envelope.trace_id,
            conversation_id_hash=pseudonymiser.conversation(envelope.conversation_id),
            message_id=message_id,
            source_agent=envelope.source_agent,
        )
        with request_context(ctx):
            try:
                result = _run(service.handle(envelope))
                ctx.state_after = result.state.value
                logger.info("turn processed", extra={"tmm_matched": result.matched})
            except Exception:
                # Retryable: let SQS redeliver this record only.
                logger.exception("turn failed")
                metrics.count(Metric.CIRCUIT_OPEN, 0)
                failures.append({"itemIdentifier": message_id})
            finally:
                metrics.flush()

    return {"batchItemFailures": failures}


# -------------------------------------------------------------- outbound worker
def outbound_worker_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """SQS -> deliver a prepared message. Never re-runs matching.

    Two failure modes are deliberately different:

    * an **unparseable** record cannot be fixed by retrying, so it is reported
      as a batch-item failure and allowed to reach the DLQ where a human sees
      it. Silently `continue`-ing (the previous behaviour) deleted a parent's
      reply with nothing but a log line to show for it;
    * a **retryable** delivery failure is reported so SQS redelivers that one
      record.
    """
    from tutor_match_meta.bootstrap import (
        build_memory,
        build_sender_from_settings,
        kill_switches,
    )
    from tutor_match_meta.config.kill_switches import Switch
    from tutor_match_meta.contracts.outbound import (
        OUTBOX_KIND_DEED,
        MemoryDeedV1,
        OutboundDeliveryV1,
        payload_kind,
    )
    from tutor_match_meta.integrations.whatsapp.outbound import OutboundMessage

    settings = get_settings()
    configure_logging(settings.log_level)

    # OUTBOUND_PAUSED holds messages in the queue rather than dropping them:
    # failing every record returns the whole batch to SQS, and its visibility
    # timeout becomes the hold. The decision is already persisted, so nothing
    # is regenerated when the pause lifts.
    if _run(kill_switches(settings).is_paused(Switch.OUTBOUND_PAUSED)):
        logger.warning("outbound paused by kill switch; holding batch")
        return {
            "batchItemFailures": [
                {"itemIdentifier": r.get("messageId", "")} for r in event.get("Records", [])
            ]
        }

    sender = build_sender_from_settings(settings)
    failures: list[dict[str, str]] = []

    deed_recorder = build_memory(settings)

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        try:
            payload = json.loads(record["body"])
            kind = payload_kind(payload)
            # A deed and a reply are different shapes. Pick the model from the
            # kind rather than validating one against the other and calling the
            # mismatch "unparseable".
            if kind == OUTBOX_KIND_DEED:
                deed = MemoryDeedV1.from_payload(payload)
            else:
                delivery = OutboundDeliveryV1.from_payload(payload)
        except Exception:
            logger.exception("unparseable outbound record", extra={"tmm_message_id": message_id})
            failures.append({"itemIdentifier": message_id})
            continue

        if kind == OUTBOX_KIND_DEED:
            # Deeds are delivered from here because this function is outside the
            # VPC; the match worker that produced it has no route to the memory
            # service at all (contracts/enrichment.py).
            with request_context(
                RequestContext(
                    trace_id=deed.trace_id or new_trace_id(),
                    conversation_id_hash=deed.conversation_ref or "n/a",
                )
            ):
                if not _run(
                    deed_recorder.record(
                        deed_type=deed.deed_type,
                        purpose=deed.purpose,
                        trace_id=deed.trace_id,
                        entity_scope=deed.entity_scope,
                        summary=deed.summary,
                    )
                ):
                    failures.append({"itemIdentifier": message_id})
            continue

        if not delivery.deliverable or not delivery.recipient:
            # An audit-only row should never reach this queue; the relay closes
            # those without sending. Reaching here means the relay and the
            # worker disagree, which is a bug worth surfacing in the DLQ.
            logger.error(
                "non-deliverable record on the outbound queue",
                extra={"tmm_kind": delivery.kind, "tmm_message_id": message_id},
            )
            failures.append({"itemIdentifier": message_id})
            continue

        message = OutboundMessage(
            to=delivery.recipient,
            body=delivery.body,
            dedup_key=delivery.dedup_key,
            trace_id=delivery.trace_id or new_trace_id(),
            preview_url=delivery.preview_url,
        )
        with request_context(
            RequestContext(
                trace_id=message.trace_id,
                conversation_id_hash=delivery.conversation_ref or "n/a",
            )
        ):
            result = _run(sender.send(message))
            if not result.delivered and result.retryable:
                failures.append({"itemIdentifier": message_id})
            elif not result.delivered:
                # Permanent failure (invalid recipient, closed 24h window).
                # Retrying wastes the DLQ signal a human needs to see.
                logger.error("permanent delivery failure", extra={"tmm_reason": result.reason})

    return {"batchItemFailures": failures}


# ------------------------------------------------------------- internal API
#: Built once per container. Mangum wraps the ASGI app; rebuilding it per
#: invocation would re-enter the FastAPI startup path on every request.
_asgi_handler: Any | None = None


def api_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """API Gateway (HTTP API v2) -> the FastAPI internal app.

    Hosts the Lead Intake handoff endpoint plus `/health`, `/ready` and
    `/version`. Kept separate from `ingress_handler` because the two have
    different auth schemes and very different blast radii: ingress can only
    enqueue, this one runs the whole matching path.
    """
    global _asgi_handler
    if _asgi_handler is None:
        from mangum import Mangum

        from tutor_match_meta.api.app import app

        settings = get_settings()
        configure_logging(settings.log_level)
        _asgi_handler = Mangum(app, lifespan="off")
    # Mangum types `context` as its own `LambdaContext` protocol; every other
    # handler here takes `object`, so the cast keeps one uniform signature
    # across the module rather than leaking a vendor type into three of four.
    result: dict[str, Any] = _asgi_handler(event, cast("Any", context))
    return result


# ------------------------------------------------------------- scheduled jobs
def scheduled_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """EventBridge -> one named maintenance job.

    Dispatch is an explicit allowlist so a malformed rule cannot invoke an
    arbitrary callable.
    """
    from tutor_match_meta.jobs import JOBS

    settings = get_settings()
    configure_logging(settings.log_level)

    job_name = str(event.get("job") or "")
    job = JOBS.get(job_name)
    if job is None:
        logger.error("unknown scheduled job", extra={"tmm_job": job_name})
        return {"ok": False, "error": f"unknown job: {job_name}", "known": sorted(JOBS)}

    with request_context(RequestContext(trace_id=new_trace_id(), conversation_id_hash="scheduled")):
        summary = _run(job(settings))
        logger.info("scheduled job finished", extra={"tmm_job": job_name})
        return {"ok": True, "job": job_name, "summary": summary}

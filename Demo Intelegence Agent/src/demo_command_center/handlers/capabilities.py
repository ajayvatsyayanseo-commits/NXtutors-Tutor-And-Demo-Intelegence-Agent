"""Per-capability Lambda entry points.

One handler per capability so each scales, fails and is throttled on its own.
They share one deployment package — separate packages would triple the build
and guarantee version skew between workers that exchange typed events.

Every handler is the same six lines of real work: parse the SQS record, build
the typed event, dispatch, classify any failure, report per-item outcome. The
business logic lives in the capability; this is the adapter.

**None of these may send a WhatsApp message.** Only `outbound_worker` holds a
sender, and `tests/security/test_boundaries.py` fails if that changes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any

from demo_command_center.bootstrap import build_dependencies, build_orchestrator
from demo_command_center.config.settings import get_settings
from demo_command_center.contracts.events import DomainEvent
from demo_command_center.glue.routing import Capability, CapabilityEvent
from demo_command_center.observability import logging as log_config
from demo_command_center.observability import metrics
from demo_command_center.resilience.errors import (
    Disposition,
    RetryPolicy,
    evaluate,
)
from demo_command_center.state.triggers import Actor, Trigger

logger = log_config.get_logger("handler.capability")

_RETRY = RetryPolicy()


def _configure() -> None:
    settings = get_settings()
    log_config.configure(settings.log_level)
    metrics.configure(enabled=settings.metrics_enabled)


def _parse(record: dict[str, Any]) -> CapabilityEvent | None:
    """SQS record → typed event. An unparseable record is dropped, not retried."""
    try:
        body = json.loads(record.get("body") or "{}")
        return CapabilityEvent(
            event=DomainEvent(body["event"]),
            capability=Capability(body["capability"]),
            conversation_ref=str(body["conversation_ref"]),
            idempotency_key=str(body["idempotency_key"]),
            trace_id=str(body.get("trace_id") or ""),
            correlation_id=str(body.get("correlation_id") or ""),
            causation_id=body.get("causation_id"),
            payload=body.get("payload") or {},
            attempt=int(body.get("attempt") or 1),
        )
    except (ValueError, KeyError, TypeError):
        # Malformed is permanent. Retrying cannot make it parse, and the DLQ is
        # where a person finds it.
        logger.warning("undecodable capability event", extra={"dcc_message": "dropped"})
        return None


def _batch(
    capability: Capability,
    handler: Callable[[CapabilityEvent], Coroutine[Any, Any, None]],
) -> Callable[[dict[str, Any], Any], dict[str, Any]]:
    """Build an SQS batch handler with per-item failure reporting.

    `batchItemFailures` is what lets one poison message be retried alone rather
    than failing the whole batch and re-running the nine that succeeded.
    """

    def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        _configure()
        failures: list[dict[str, str]] = []

        for record in event.get("Records") or []:
            message_id = str(record.get("messageId") or "")
            parsed = _parse(record)
            if parsed is None:
                # Deliberately NOT a batch failure: redelivering an unparseable
                # record just burns the redrive count to reach the same DLQ.
                metrics.emit(metrics.Metric.WEBHOOK_REJECTED, reason="undecodable")
                continue

            log_config.bind(
                trace_id=parsed.trace_id,
                correlation_id=parsed.correlation_id,
                conversation_ref=parsed.conversation_ref,
                capability=capability.value,
                handler=capability.value,
            )
            try:
                asyncio.run(handler(parsed))
            except Exception as error:
                outcome = evaluate(error, attempt=parsed.attempt, policy=_RETRY)
                metrics.emit(
                    metrics.Metric.PROVIDER_ERROR,
                    capability=capability.value,
                    error_class=outcome.error_class.value,
                )
                logger.warning(
                    "capability event failed",
                    extra={
                        "dcc_error_class": outcome.error_class.value,
                        "dcc_disposition": outcome.disposition.value,
                        "dcc_attempt": str(parsed.attempt),
                    },
                )
                if outcome.disposition is Disposition.RETRY:
                    failures.append({"itemIdentifier": message_id})
                elif outcome.disposition is Disposition.HUMAN_REVIEW:
                    asyncio.run(_raise_case(parsed, outcome.error_class.value))
                # DEAD_LETTER and TERMINAL_FAILURE: not re-reported, so SQS
                # deletes the message and its redrive policy has already
                # routed the copies that mattered.
            finally:
                log_config.reset_context()

        return {"batchItemFailures": failures}

    return lambda_handler


async def _raise_case(event: CapabilityEvent, reason: str) -> None:
    """Open a human case for a failure no retry will fix."""
    from demo_command_center.human_handoff.escalation import (
        EscalationTrigger,
        build_packet,
    )

    deps = build_dependencies()
    packet = build_packet(
        case_id=f"hc_{event.idempotency_key[:16]}",
        conversation_ref=event.conversation_ref,
        trigger=EscalationTrigger.POISON_EVENT,
        state=(await deps.conversations.load(event.conversation_ref)).state,
        now=deps.clock.now(),
        problem=f"{event.event.value} could not be processed: {reason}",
        attempted=(f"{event.attempt} attempt(s)",),
    )
    await deps.operations.open_human_case(packet.as_row())
    metrics.emit(metrics.Metric.HUMAN_HANDOFF, reason=reason[:40])


# ------------------------------------------------------------ the dispatchers


async def _via_orchestrator(event: CapabilityEvent, trigger: Trigger) -> None:
    """Most capability work is a trigger on the one state machine.

    Deliberately routed through the orchestrator rather than calling the
    capability directly: that is what keeps ownership, guards, idempotency and
    the single outbound boundary on the path for every worker.
    """
    await build_orchestrator().handle(
        conversation_ref=event.conversation_ref,
        trigger=trigger,
        actor=Actor.SYSTEM,
        event_id=event.idempotency_key,
        payload=event.payload or {},
        trace_id=event.trace_id or None,
        causation_id=event.causation_id,
    )


async def _scheduling(event: CapabilityEvent) -> None:
    trigger = {
        DomainEvent.TUTOR_SELECTED: Trigger.SLOT_PROPOSED,
        DomainEvent.SLOT_HELD: Trigger.HOLD_PLACED,
        DomainEvent.SLOT_HOLD_EXPIRED: Trigger.HOLD_EXPIRED,
    }.get(event.event)
    if trigger is not None:
        await _via_orchestrator(event, trigger)


async def _reminders(event: CapabilityEvent) -> None:
    if event.event in (DomainEvent.SCHEDULED, DomainEvent.RESCHEDULED):
        await _via_orchestrator(event, Trigger.REMINDERS_SCHEDULED)
    elif event.event is DomainEvent.CANCELLED:
        deps = build_dependencies()
        demo_id = str((event.payload or {}).get("demo_id") or "")
        if demo_id:
            await deps.reminders.cancel_for_demo(demo_id)


async def _objections(event: CapabilityEvent) -> None:
    if event.event is DomainEvent.COMPLETED:
        await _via_orchestrator(event, Trigger.ANALYSIS_REQUESTED)


async def _forecasting(event: CapabilityEvent) -> None:
    """Analytics only. Never blocks the customer-facing path."""
    if event.event is DomainEvent.OBJECTIONS_EXTRACTED:
        await _via_orchestrator(event, Trigger.ANALYSIS_COMPLETE)


async def _conversion(event: CapabilityEvent) -> None:
    if event.event in (DomainEvent.FORECAST_UPDATED, DomainEvent.DISCOUNT_OFFERED):
        await _via_orchestrator(event, Trigger.ANALYSIS_COMPLETE)
    elif event.event is DomainEvent.PAYMENT_FAILED:
        await _via_orchestrator(event, Trigger.FOLLOWUP_SENT)


async def _discounts(event: CapabilityEvent) -> None:
    """The engine runs here, on its own lane, and never in a webhook."""
    deps = build_dependencies()
    demo = await deps.demos.for_conversation(event.conversation_ref)
    if demo is None:
        return
    quote = await deps.gateway.plan_quote(student_ref=demo.student_ref, plan_ref=None)
    analysis = await deps.analysis.load_objections(demo.demo_id)
    decision = deps.discounts.evaluate(
        conversation_ref=event.conversation_ref,
        demo_id=demo.demo_id,
        student_ref=demo.student_ref,
        quote=quote,
        analysis=analysis,
        outcome=demo.outcome.outcome,
        prior_offers=await deps.commerce.offers_since(
            student_ref=demo.student_ref, since=deps.clock.now().replace(month=1, day=1)
        ),
        repeat_requests=int((event.payload or {}).get("repeat_requests") or 0),
        now=deps.clock.now(),
        enabled=deps.discounts_enabled,
    )
    await deps.commerce.save_decision(decision)
    metrics.emit(
        metrics.Metric.DISCOUNT_APPROVED
        if decision.status.value == "approved"
        else metrics.Metric.DISCOUNT_DENIED,
        band=decision.band_name or "none",
    )


async def _paid_transition(event: CapabilityEvent) -> None:
    trigger = {
        DomainEvent.PAYMENT_REQUESTED: Trigger.PAYMENT_LINK_ISSUED,
        DomainEvent.PAYMENT_CONFIRMED: Trigger.ACTIVATION_STARTED,
        DomainEvent.SUBSCRIPTION_ACTIVATED: Trigger.ACTIVATION_SUCCEEDED,
        DomainEvent.ONBOARDING_REQUESTED: Trigger.ONBOARDING_HANDED_OFF,
    }.get(event.event)
    if trigger is not None:
        await _via_orchestrator(event, trigger)


async def _monitoring(event: CapabilityEvent) -> None:
    """Rollups and alerts. Reads widely, writes only its own tables."""
    deps = build_dependencies()
    region = str((event.payload or {}).get("region") or "")
    if not region:
        return
    authorised = await deps.gateway.region_authorization(operator_ref="system")
    rollup = await deps.monitoring.rollup(
        region=region, authorised_regions=authorised, now=deps.clock.now()
    )
    for metric in ("student_no_show_rate", "tutor_no_show_rate", "conversion_rate"):
        await deps.operations.save_rollup(rollup.as_row(metric))
    for alert in await deps.monitoring.evaluate(metrics=rollup, now=deps.clock.now()):
        metrics.emit(
            metrics.Metric.UNDERPERFORMANCE_ALERT, region=alert.region, severity=alert.severity
        )


# ------------------------------------------------------------- the exports
#
# One Lambda function each. Named for the Terraform `handler` values.

demo_scheduling_worker = _batch(Capability.SCHEDULING, _scheduling)
demo_reminder_worker = _batch(Capability.REMINDERS, _reminders)
demo_objection_worker = _batch(Capability.OBJECTIONS, _objections)
demo_forecast_worker = _batch(Capability.FORECASTING, _forecasting)
demo_conversion_worker = _batch(Capability.CONVERSION, _conversion)
demo_discount_worker = _batch(Capability.DISCOUNTS, _discounts)
demo_paid_transition_worker = _batch(Capability.PAID_TRANSITION, _paid_transition)
demo_monitoring_worker = _batch(Capability.MONITORING, _monitoring)

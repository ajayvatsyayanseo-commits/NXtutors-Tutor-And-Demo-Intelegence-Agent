"""SQS and scheduler workers, plus the internal handoff entry point.

Where the actual work happens. Every one of these is a thin adapter: parse the
AWS event shape, translate it to a `(conversation_ref, trigger, actor)` triple,
and hand it to the orchestrator. No business logic lives here, which is why the
services are covered by tests and these are not.

`batchItemFailures` is the important detail on the SQS path. Returning it lets
one poison message be retried on its own instead of failing the whole batch and
re-running the nine that already succeeded.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from demo_command_center.bootstrap import build_dependencies, build_orchestrator
from demo_command_center.config.settings import get_settings
from demo_command_center.contracts.common import Party
from demo_command_center.observability import logging as log_config
from demo_command_center.observability import metrics
from demo_command_center.orchestration.orchestrator import (
    DemoCommandCenterOrchestrator,
    TurnResult,
)
from demo_command_center.security.signatures import (
    INTERNAL_SIGNATURE_HEADER,
    INTERNAL_TIMESTAMP_HEADER,
    SignatureError,
    SignedRequest,
    parse_timestamp,
    verify_internal,
)
from demo_command_center.state.triggers import Actor, Trigger

logger = log_config.get_logger("handler.worker")


def work_queue(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Process a batch of work items, reporting per-item failures."""
    log_config.configure(get_settings().log_level)
    failures: list[dict[str, str]] = []

    for record in event.get("Records") or []:
        message_id = str(record.get("messageId") or "")
        try:
            asyncio.run(_handle_record(json.loads(record.get("body") or "{}")))
        except Exception:
            logger.exception("work item failed", extra={"dcc_message_id": message_id})
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


async def _handle_record(body: dict[str, Any]) -> TurnResult | None:
    orchestrator = build_orchestrator()
    conversation_ref = str(body.get("conversation_ref") or "")
    if not conversation_ref:
        logger.warning("work item missing conversation_ref")
        return None

    if str(body.get("kind") or "") == "inbound_message":
        # A parent's message carries text, not a trigger. Routing happens here
        # rather than in the webhook because deciding what a message means
        # requires the conversation state, and the internet-facing ingress
        # function deliberately holds no database grant.
        return await _handle_inbound_message(orchestrator, conversation_ref, body)

    raw_trigger = str(body.get("trigger") or "")
    if not raw_trigger:
        logger.warning("work item missing trigger")
        return None

    try:
        trigger = Trigger(raw_trigger)
        actor = Actor(str(body.get("actor") or Actor.SYSTEM.value))
    except ValueError:
        # An unknown trigger is a routing bug or an old message from a previous
        # deploy. Dropping is right — retrying cannot make it known.
        logger.warning(
            "work item names an unknown trigger", extra={"dcc_trigger": raw_trigger[:40]}
        )
        return None

    return await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=trigger,
        actor=actor,
        event_id=str(body.get("event_id") or body.get("dedup") or raw_trigger),
        payload=body.get("payload") or {},
        trace_id=str(body.get("trace_id") or "") or None,
        causation_id=str(body.get("causation_id") or "") or None,
    )


def scheduled_callback(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """EventBridge Scheduler one-shot: reminders, hold expiry, tutor expiry."""
    log_config.configure(get_settings().log_level)
    result = asyncio.run(_handle_record({**event, "actor": Actor.SCHEDULER.value}))
    return {"state": result.state if result else "ignored"}


def reminder_sweep(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Periodic sweep for due reminders and lapsed holds.

    A belt-and-braces companion to the per-reminder schedules: a schedule that
    failed to register leaves a row nobody would ever fire, and this finds it.
    """
    log_config.configure(get_settings().log_level)
    return asyncio.run(_sweep())


async def _sweep() -> dict[str, Any]:
    deps = build_dependencies()
    now = deps.clock.now()

    expired = await deps.slots.expire_due(now=now)
    due = await deps.reminders.due(now=now, limit=100)

    sent = 0
    for reminder in due:
        demo = await deps.demos.load(reminder.demo_id)
        if demo is None:
            await deps.reminders.mark(
                reminder.reminder_id, status="cancelled", now=now, detail="demo_missing"
            )
            continue
        reason = deps.reminder_policy.obsolete(reminder, demo, now=now)
        if reason is not None:
            await deps.reminders.mark(
                reminder.reminder_id, status="suppressed", now=now, detail=reason
            )
            continue

        message = deps.reminder_policy.to_message(
            reminder,
            demo=demo,
            variables=_reminder_variables(reminder, demo),
        )
        outcome = await deps.outbound.send(message)
        await deps.reminders.mark(
            reminder.reminder_id,
            status="sent" if outcome.delivered else "suppressed",
            now=now,
            detail=outcome.detail or outcome.outcome.value,
        )
        sent += int(outcome.delivered)

    metrics.emit(metrics.Metric.REMINDER_SENT, float(sent))
    return {"expired_holds": len(expired), "reminders_due": len(due), "reminders_sent": sent}


def _reminder_variables(reminder: Any, demo: Any) -> tuple[str, ...]:
    """Positional template variables, in the registry's declared order.

    Built from the template's own declaration rather than a per-label branch, so
    adding a variable to an approved template is a registry edit and not a
    scattered set of tuple literals that silently disagree.
    """
    from demo_command_center.contracts.common import Party
    from demo_command_center.integrations.meta_whatsapp.templates import (
        TemplateNotApproved,
        registry,
    )

    try:
        template = registry().get(reminder.template)
    except TemplateNotApproved:
        return ()

    student = demo.attendee(Party.STUDENT)
    tutor = demo.attendee(Party.TUTOR)
    available = {
        # `label_without_zone`, not `label`: every approved reminder template
        # renders the zone as its own field, so `label` would print IST twice.
        "demo_datetime": demo.slot.label_without_zone() if demo.slot else "",
        "timezone": demo.slot.timezone if demo.slot else "",
        # The demo id, unshortened and identical to the one on the calendar
        # event, so a parent quoting it to support resolves to exactly one demo.
        "reference": demo.demo_id,
        "join_link": demo.meet_url or demo.location_label or "",
        # Neither name appears in any currently approved template. Kept so that
        # approving a personalised variant is a registry edit and not a code
        # change — and so the lookup below still resolves if one is added.
        "student_name": (student.display_name if student else "") or "there",
        "tutor_name": tutor.display_name if tutor else "",
    }
    return tuple(available.get(name, "") or "-" for name in template.variables)


async def _handle_inbound_message(
    orchestrator: DemoCommandCenterOrchestrator,
    conversation_ref: str,
    body: dict[str, Any],
) -> TurnResult | None:
    """Route one parent message onto a trigger and fire it.

    An unroutable message is answered, not dropped. Silence is the worst
    outcome here: the parent has no idea whether anyone read it, and the
    conversation stalls with no error anywhere to explain why.
    """
    from demo_command_center.orchestration.inbound import clarification, route
    from demo_command_center.state.machine import StateMachine

    deps = build_dependencies()
    snapshot = await deps.conversations.load(conversation_ref)
    routed = route(
        text=str(body.get("text") or ""),
        button_id=str(body.get("button_id") or "") or None,
        snapshot=snapshot,
        machine=StateMachine(),
    )

    metrics.emit(
        metrics.Metric.INBOUND_ROUTED if routed.understood else metrics.Metric.INBOUND_UNROUTED,
        reason=routed.reason[:40],
    )

    if not routed.understood or routed.trigger is None:
        logger.info(
            "inbound message not routable",
            extra={"dcc_reason": routed.reason[:60], "dcc_state": snapshot.state.value},
        )
        await _ask_for_clarification(deps, conversation_ref, clarification(routed, snapshot))
        return None

    return await orchestrator.handle(
        conversation_ref=conversation_ref,
        trigger=routed.trigger,
        actor=Actor.USER,
        event_id=str(body.get("event_id") or body.get("wa_message_id") or ""),
        payload=routed.payload,
    )


async def _ask_for_clarification(deps: Any, conversation_ref: str, text: str) -> None:
    """Send the "I did not follow" reply through the one outbound boundary.

    Through the boundary, never around it: this reply is still subject to
    opt-out, the session window, rate limits and the output guard exactly like
    every other message.
    """
    from demo_command_center.domain.messages import MessageKind, OutboundMessage
    from demo_command_center.security.signatures import idempotency_key

    now = deps.clock.now()
    try:
        await deps.outbound.send(
            OutboundMessage(
                conversation_ref=conversation_ref,
                recipient_ref=conversation_ref,
                audience=Party.STUDENT,
                kind=MessageKind.QUESTION,
                body=text,
                idempotency_key=idempotency_key("clarify", conversation_ref, text),
                created_at=now,
            )
        )
    except Exception as exc:  # a failed clarification must not poison the batch
        logger.warning("clarification not sent", extra={"dcc_error": type(exc).__name__})


def outbox_relay(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Publish unpublished outbox rows. The "row written, event lost" fix."""
    log_config.configure(get_settings().log_level)
    return asyncio.run(_relay())


async def _relay() -> dict[str, Any]:
    deps = build_dependencies()
    now = deps.clock.now()
    rows = await deps.outbox.unpublished(limit=100)
    published = 0

    for row in rows:
        try:
            if str(row["event"]) == "onboarding.requested" and deps.onboarding_webhook_url:
                await deps.agents.dispatch(
                    {
                        "schema_version": "1.0",
                        "source_agent": "demo_command_center_agent",
                        "destination_agent": "onboarding_agent",
                        "event_type": row["event"],
                        "idempotency_key": row["idempotency_key"],
                        "payload": row["payload"],
                    },
                    url=deps.onboarding_webhook_url,
                )
            await deps.outbox.mark_published(str(row["outbox_id"]), now=now)
            published += 1
        except Exception:
            logger.warning("outbox publish failed", extra={"dcc_event": str(row["event"])})
            metrics.emit(metrics.Metric.OUTBOX_FAILED, event=str(row["event"]))

    metrics.emit(metrics.Metric.OUTBOX_PUBLISHED, float(published))
    return {"pending": len(rows), "published": published}


def internal_handoff(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """The signed entry point another NXTutors agent calls to hand us a lead."""
    settings = get_settings()
    log_config.configure(settings.log_level)

    raw = (event.get("body") or "").encode("utf-8")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    path = str(event.get("rawPath") or event.get("path") or "/internal/handoff")

    try:
        verify_internal(
            secret=settings.internal_signing_secret.get_secret_value(),
            request=SignedRequest(
                method="POST",
                path=path,
                timestamp=parse_timestamp(headers.get(INTERNAL_TIMESTAMP_HEADER)),
                body=raw,
            ),
            provided=headers.get(INTERNAL_SIGNATURE_HEADER),
            tolerance_seconds=settings.internal_timestamp_tolerance_seconds,
            max_body_bytes=settings.max_body_bytes,
        )
    except SignatureError as exc:
        metrics.emit(metrics.Metric.WEBHOOK_SIGNATURE_FAILURE, reason=exc.reason.value)
        return {"statusCode": 401, "body": json.dumps({"status": "unauthorized"})}

    try:
        body = json.loads(raw)
    except ValueError:
        return {"statusCode": 400, "body": json.dumps({"status": "bad_request"})}

    deps = build_dependencies()
    conversation_ref = deps.pseudonymiser.conversation(
        str(body.get("conversation_id") or body.get("wa_phone") or "")
    )
    result = asyncio.run(
        build_orchestrator().handle(
            conversation_ref=conversation_ref,
            trigger=Trigger.HANDOFF_RECEIVED,
            actor=Actor.AGENT,
            event_id=str(body.get("wa_message_id") or body.get("event_id") or ""),
            payload={"phone_hash": deps.pseudonymiser.phone(str(body.get("wa_phone") or ""))},
            trace_id=str(body.get("trace_id") or "") or None,
        )
    )

    # Always 200 with a status in the body. A non-2xx makes the caller retry,
    # and retrying a handoff we deliberately declined would loop.
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(
            {
                "status": "accepted" if result.accepted else "declined",
                # Under `self_sends` we deliver our own messages, so we return
                # no text for the caller to send — that would be a double send.
                "reply_text": None,
                "duplicate": result.duplicate,
                "state": result.state,
            }
        ),
    }

"""Public webhook handlers: Meta and Cashfree.

Both do the same four things and nothing else:

1. Verify the signature **over the raw bytes that arrived**.
2. Deduplicate on the provider's own event id.
3. Enqueue.
4. Return 200 fast.

**No LLM call, no database write beyond dedup, no business logic runs here.**
Meta retries anything slower than a few seconds, so a slow webhook turns one
slow turn into a redelivery storm that re-runs the same expensive work. The
queue is what breaks that loop.

A rejected signature returns 403 and is never enqueued. A *duplicate* returns
200 — telling a provider "error" for something we have already handled makes it
retry forever.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from demo_command_center.bootstrap import build_dependencies
from demo_command_center.config.settings import get_settings
from demo_command_center.observability import logging as log_config
from demo_command_center.observability import metrics
from demo_command_center.security.guardrails import InputRejected, assert_depth_ok
from demo_command_center.security.signatures import (
    CASHFREE_SIGNATURE_HEADER,
    CASHFREE_TIMESTAMP_HEADER,
    META_SIGNATURE_HEADER,
    SignatureError,
    verify_cashfree,
    verify_meta,
    verify_meta_challenge,
)
from demo_command_center.storage.queue import build_publisher

logger = log_config.get_logger("handler.webhook")


def _response(status: int, body: dict[str, Any] | str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json" if isinstance(body, dict) else "text/plain"},
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }


def _raw_body(event: dict[str, Any]) -> bytes:
    """The exact bytes that arrived.

    API Gateway base64-encodes a body it considers binary. Decoding the string
    form and re-encoding it would change the bytes, and the signature would then
    fail for a payload that is perfectly valid.
    """
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode("utf-8")


def _headers(event: dict[str, Any]) -> dict[str, str]:
    return {key.lower(): value for key, value in (event.get("headers") or {}).items()}


# ---------------------------------------------------------------- Meta


def meta_webhook(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """GET verifies the subscription; POST accepts messages and statuses."""
    settings = get_settings()
    log_config.configure(settings.log_level)
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    ).upper()

    if method == "GET":
        params = event.get("queryStringParameters") or {}
        if verify_meta_challenge(
            verify_token=settings.meta_verify_token.get_secret_value(),
            mode=params.get("hub.mode"),
            token=params.get("hub.verify_token"),
        ):
            return _response(200, params.get("hub.challenge", ""))
        metrics.emit(metrics.Metric.WEBHOOK_REJECTED, reason="challenge_failed")
        return _response(403, {"status": "forbidden"})

    raw = _raw_body(event)
    headers = _headers(event)
    try:
        verify_meta(
            app_secret=settings.meta_app_secret.get_secret_value(),
            raw_body=raw,
            provided=headers.get(META_SIGNATURE_HEADER),
            max_body_bytes=settings.max_body_bytes,
        )
    except SignatureError as exc:
        metrics.emit(metrics.Metric.WEBHOOK_SIGNATURE_FAILURE, reason=exc.reason.value)
        return _response(403, {"status": "invalid_signature"})

    try:
        payload = json.loads(raw)
        assert_depth_ok(payload)
    except (ValueError, InputRejected):
        metrics.emit(metrics.Metric.WEBHOOK_REJECTED, reason="malformed_body")
        # 200, not 400: Meta retries a 4xx, and a malformed body will be just as
        # malformed next time.
        return _response(200, {"status": "ignored"})

    metrics.emit(metrics.Metric.WEBHOOK_ACCEPTED, source="meta")
    return _response(200, {"status": "accepted", "queued": _enqueue_meta(payload)})


def _enqueue_meta(payload: dict[str, Any]) -> int:
    """Flatten Meta's nested envelope into one work item per message.

    Meta batches: one webhook can carry several messages for several numbers.
    Enqueuing the batch would make one poison message block the rest.

    **No database call happens here, deliberately.** This is the only
    internet-facing function in the system and its IAM role grants it no
    database access at all — see the `ingress` policy in `infra/terraform/iam.tf`,
    which says "verified and enqueued only". An idempotency claim used to run
    here, which would have been an AccessDenied on every webhook in `data_api`
    mode, and needlessly widened the blast radius of the one function an
    attacker can reach.

    Replay protection is not lost, it moved to the two places that already had
    it: the FIFO queue deduplicates on `MessageDeduplicationId` for five
    minutes, which covers Meta's own redelivery behaviour, and the orchestrator
    claims on `(conversation_ref, event_id, trigger)` for anything later.
    """
    import asyncio

    deps = build_dependencies()
    items: list[dict[str, Any]] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                items.append({"kind": "inbound_message", "message": message, "meta": value})
            for status in value.get("statuses") or []:
                items.append({"kind": "delivery_status", "status": status})

    settings = get_settings()
    publisher = build_publisher(settings)

    async def publish() -> int:
        sent = 0
        for item in items:
            dedup = _meta_dedup(item)
            work = _work_item(item, deps)
            if work is None:
                continue

            # This publish is what was missing. Without it the batch was
            # flattened, deduplicated and dropped: the webhook answered 200,
            # every metric looked healthy, and the orchestrator never ran.
            await publisher.publish(
                queue_url=settings.work_queue_url,
                body=work,
                group_id=str(work.get("conversation_ref") or "default"),
                dedup_id=dedup,
            )
            sent += 1
        return sent

    return asyncio.run(publish())


def _work_item(item: dict[str, Any], deps: Any) -> dict[str, Any] | None:
    """One Meta entry as a work-queue item, or None when it carries no turn.

    The trigger is deliberately NOT decided here. This handler is the only
    internet-facing function in the system and holds no database grant, so it
    cannot load the conversation state that decides what a message means. It
    forwards the text and the worker — which does have that state — routes it
    through `orchestration.inbound.route`.
    """
    kind = str(item.get("kind") or "")

    if kind == "delivery_status":
        status = item.get("status") or {}
        return {
            "kind": "delivery_status",
            "conversation_ref": deps.pseudonymiser.phone(str(status.get("recipient_id") or "")),
            "provider_message_id": str(status.get("id") or ""),
            "status": str(status.get("status") or ""),
            "event_id": str(status.get("id") or ""),
        }

    message = item.get("message") or {}
    sender = str(message.get("from") or "")
    if not sender:
        return None

    # The conversation is keyed on the pseudonymised number, never the number
    # itself: the same identity Lead Intake hands off under, and nothing
    # downstream ever stores a raw phone.
    conversation_ref = deps.pseudonymiser.phone(sender)

    text = ""
    button_id = ""
    message_type = str(message.get("type") or "")
    if message_type == "text":
        text = str((message.get("text") or {}).get("body") or "")
    elif message_type == "interactive":
        interactive = message.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        button_id = str(reply.get("id") or "")
        text = str(reply.get("title") or "")
    elif message_type == "button":
        button = message.get("button") or {}
        button_id = str(button.get("payload") or "")
        text = str(button.get("text") or "")
    else:
        # Image, audio, location and the rest carry no routable intent. Passed
        # through with empty text so the worker asks a clarifying question
        # rather than the message vanishing.
        text = ""

    return {
        "kind": "inbound_message",
        "conversation_ref": conversation_ref,
        "text": text[: get_settings().max_body_bytes],
        "button_id": button_id,
        "wa_message_id": str(message.get("id") or ""),
        "event_id": str(message.get("id") or ""),
        "received_at": str(message.get("timestamp") or ""),
    }


def _meta_dedup(item: dict[str, Any]) -> str:
    from demo_command_center.security.signatures import idempotency_key

    body = item.get("message") or item.get("status") or {}
    return idempotency_key("meta", str(item.get("kind")), str(body.get("id")))


# ------------------------------------------------------------ Cashfree


def cashfree_webhook(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """A verified server-to-server payment event. Never a customer's claim."""
    import asyncio

    settings = get_settings()
    log_config.configure(settings.log_level)

    raw = _raw_body(event)
    headers = _headers(event)
    try:
        verify_cashfree(
            secret_key=settings.cashfree_secret_key.get_secret_value(),
            raw_body=raw,
            timestamp=headers.get(CASHFREE_TIMESTAMP_HEADER),
            provided=headers.get(CASHFREE_SIGNATURE_HEADER),
            max_body_bytes=settings.max_body_bytes,
            tolerance_seconds=settings.cashfree_webhook_tolerance_seconds,
        )
    except SignatureError as exc:
        metrics.emit(metrics.Metric.WEBHOOK_SIGNATURE_FAILURE, reason=exc.reason.value)
        return _response(403, {"status": "invalid_signature"})

    try:
        payload = json.loads(raw)
        assert_depth_ok(payload)
    except (ValueError, InputRejected):
        metrics.emit(metrics.Metric.WEBHOOK_REJECTED, reason="malformed_body")
        return _response(200, {"status": "ignored"})

    event_model = parse_cashfree_event(payload, raw_digest=_digest(raw))
    if event_model is None:
        return _response(200, {"status": "ignored"})

    deps = build_dependencies()
    result = asyncio.run(deps.paid.accept_webhook(event_model))

    if result.duplicate:
        metrics.emit(metrics.Metric.WEBHOOK_DUPLICATE, source="cashfree")
        return _response(200, {"status": "duplicate"})
    if not result.accepted:
        metrics.emit(metrics.Metric.PAYMENT_MISMATCH, reason=result.reason[:40])
        # 200 so Cashfree stops retrying; the mismatch is alarmed on our side
        # because it needs a human, not another delivery attempt.
        return _response(200, {"status": "rejected", "reason": result.reason})

    metrics.emit(metrics.Metric.PAYMENT_VERIFIED)
    return _response(200, {"status": "accepted"})


def parse_cashfree_event(payload: dict[str, Any], *, raw_digest: str) -> Any:
    """Cashfree's nested body → a `PaymentEvent`, or None if unrecognised.

    Amounts arrive as major-unit floats. They are converted through `Decimal`,
    never through `float * 100`, because `4799.99 * 100` is `479998.99999...`
    and truncates to a paise short — which the reconciler would then reject as
    an amount mismatch on a perfectly good payment.
    """
    from datetime import UTC, datetime
    from decimal import ROUND_HALF_UP, Decimal

    from demo_command_center.domain.payments import PaymentEvent, PaymentEventKind

    data = payload.get("data") or {}
    order = data.get("order") or {}
    payment = data.get("payment") or {}
    order_ref = str(order.get("order_id") or "")
    if not order_ref:
        return None

    kind = {
        "PAYMENT_SUCCESS_WEBHOOK": PaymentEventKind.SUCCESS,
        "PAYMENT_FAILED_WEBHOOK": PaymentEventKind.FAILED,
        "PAYMENT_USER_DROPPED_WEBHOOK": PaymentEventKind.USER_DROPPED,
    }.get(str(payload.get("type") or ""), PaymentEventKind.UNKNOWN)

    raw_amount = payment.get("payment_amount", order.get("order_amount", 0))
    minor = int((Decimal(str(raw_amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    occurred_raw = str(payload.get("event_time") or "")
    try:
        occurred = datetime.fromisoformat(occurred_raw)
    except ValueError:
        occurred = datetime.now(UTC)
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)

    return PaymentEvent(
        provider_event_id=str(payment.get("cf_payment_id") or f"{order_ref}:{payload.get('type')}"),
        kind=kind,
        order_ref=order_ref,
        amount_minor=minor,
        currency=str(payment.get("payment_currency") or order.get("order_currency") or "INR"),
        provider_reference=str(payment.get("bank_reference") or ""),
        occurred_at=occurred,
        # Set here, and only here — after `verify_cashfree` returned.
        signature_verified=True,
        raw_digest=raw_digest,
    )


def _digest(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()[:64]

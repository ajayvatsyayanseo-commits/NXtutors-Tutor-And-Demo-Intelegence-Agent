"""The single outbound boundary. Nothing else in this service may send.

Every capability returns `OutboundMessage` values; this is the only module that
turns one into a delivered WhatsApp message. Centralising it is what makes the
eight capabilities safe to run as separate Lambdas — a worker that finishes late
cannot talk over a human who took the conversation, because the checks happen
here, at send time, not there, at decide time.

The gate order is deliberate and each step is cheaper than the next:

1. **Expiry** — a T-15m reminder that arrives after the demo is dropped.
2. **Ownership** — are we still the owner? One row read.
3. **State** — terminal and suspended conversations send nothing.
4. **Opt-out** — with a deliberately small transactional exemption.
5. **Idempotency claim** — the durable single-winner check. Everything after
   this point costs money or reputation, so this is the last free gate.
6. **Session window / template** — free-form is only legal inside 24h.
7. **Output guardrail** — no PII, no unapproved URL, no leaked internals.
8. **Rate limit** — durable, per recipient.
9. **Send.**

`tests/security/test_single_sender.py` asserts that no module outside this one
imports a `WhatsAppPort`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from demo_command_center.contracts.ownership import OwnershipError
from demo_command_center.contracts.ports import ContactResolverPort, ProviderError, WhatsAppPort
from demo_command_center.domain.messages import (
    TERMINAL_ALLOWED,
    OutboundMessage,
    SendOutcome,
    SendResult,
    session_window_open,
)
from demo_command_center.guardrails.output import OutputGuard
from demo_command_center.observability import metrics
from demo_command_center.observability.logging import get_logger
from demo_command_center.repositories.ports import ConversationRepository, MessageLogRepository
from demo_command_center.security.rate_limit import Limiter, LimitScope
from demo_command_center.shared.clock import Clock
from demo_command_center.state.states import SUSPENDED, can_send_automated_message

logger = get_logger("outbound")


@dataclass(slots=True)
class OutboundPolicy:
    """Everything the boundary needs that is configuration rather than state."""

    session_window_hours: int = 24
    sends_per_identity_per_hour: int = 20
    #: A template that is not in the registry is refused rather than sent as
    #: free text, which Meta would reject anyway — silently, hours later.
    require_approved_template: bool = True


class OutboundBoundary:
    def __init__(
        self,
        *,
        sender: WhatsAppPort,
        contacts: ContactResolverPort,
        conversations: ConversationRepository,
        message_log: MessageLogRepository,
        guard: OutputGuard,
        limiter: Limiter,
        clock: Clock,
        policy: OutboundPolicy | None = None,
        template_names: frozenset[str] = frozenset(),
    ) -> None:
        self._sender = sender
        self._contacts = contacts
        self._conversations = conversations
        self._log = message_log
        self._guard = guard
        self._limiter = limiter
        self._clock = clock
        self._policy = policy or OutboundPolicy()
        self._templates = template_names

    async def send(self, message: OutboundMessage) -> SendResult:
        """The only send path. Returns an outcome; never raises for policy."""
        now = self._clock.now()

        def refuse(outcome: SendOutcome, detail: str = "") -> SendResult:
            if outcome is not SendOutcome.DUPLICATE:
                metrics.emit(
                    metrics.Metric.REMINDER_SUPPRESSED
                    if message.kind.value == "reminder"
                    else metrics.Metric.WEBHOOK_REJECTED,
                    reason=outcome.value,
                )
            logger.info(
                "outbound suppressed",
                extra={"dcc_outcome": outcome.value, "dcc_kind": message.kind.value},
            )
            return SendResult(
                outcome=outcome, idempotency_key=message.idempotency_key, detail=detail[:200]
            )

        # 1. expiry
        if message.expired(now=now):
            return refuse(SendOutcome.SUPPRESSED_EXPIRED)

        # 2. ownership
        ownership = await self._conversations.load_ownership(message.conversation_ref, now=now)
        try:
            ownership.assert_may_send(now=now)
        except OwnershipError as exc:
            return refuse(SendOutcome.SUPPRESSED_NOT_OWNER, exc.reason)

        # 3. state
        #
        # Suspended always blocks — a human or another agent holds the
        # conversation and must not be talked over. Terminal blocks too, except
        # for the message that announces the terminal state itself.
        snapshot = await self._conversations.load(message.conversation_ref)
        if snapshot.state in SUSPENDED:
            return refuse(SendOutcome.SUPPRESSED_STATE, snapshot.state.value)
        if not can_send_automated_message(snapshot.state) and (
            message.kind not in TERMINAL_ALLOWED
        ):
            return refuse(SendOutcome.SUPPRESSED_STATE, snapshot.state.value)

        # 4. opt-out
        if not message.opt_out_exempt and await self._contacts.opted_out(message.recipient_ref):
            return refuse(SendOutcome.SUPPRESSED_OPT_OUT)

        # 5. idempotency — the durable single-winner claim
        if not await self._log.claim_send(message, now=now):
            return SendResult(
                outcome=SendOutcome.DUPLICATE, idempotency_key=message.idempotency_key
            )

        # 6. session window vs template
        refusal = await self._template_check(message, now=now)
        if refusal is not None:
            result = refuse(SendOutcome.SUPPRESSED_NO_TEMPLATE, refusal)
            await self._log.record_result(
                idempotency_key=message.idempotency_key, result=result, now=now
            )
            return result

        # 7. output guardrail
        verdict = self._guard.check(message)
        if not verdict.allowed:
            logger.error(
                "outbound blocked by guardrail",
                extra={"dcc_violations": ",".join(verdict.violations)[:200]},
            )
            result = refuse(SendOutcome.SUPPRESSED_GUARDRAIL, ",".join(verdict.violations))
            await self._log.record_result(
                idempotency_key=message.idempotency_key, result=result, now=now
            )
            return result

        # 8. rate limit, per recipient
        decision = await self._limiter.check(
            LimitScope.WHATSAPP_SEND,
            message.recipient_ref,
            limit=self._policy.sends_per_identity_per_hour,
            per_seconds=3600,
        )
        if not decision.allowed:
            result = refuse(
                SendOutcome.SUPPRESSED_RATE_LIMIT, f"retry_in={decision.retry_after_seconds}"
            )
            await self._log.record_result(
                idempotency_key=message.idempotency_key, result=result, now=now
            )
            return result

        # 9. resolve and send. The phone number exists only inside this block.
        recipient = await self._contacts.resolve(message.recipient_ref)
        if not recipient:
            result = refuse(SendOutcome.SUPPRESSED_OPT_OUT, "unresolvable_recipient")
            await self._log.record_result(
                idempotency_key=message.idempotency_key, result=result, now=now
            )
            return result

        try:
            result = await self._sender.send(verdict.message, recipient=recipient)
        except ProviderError as exc:
            logger.warning("whatsapp send failed", extra={"dcc_code": exc.code})
            result = SendResult(
                outcome=SendOutcome.FAILED,
                idempotency_key=message.idempotency_key,
                detail=f"{exc.provider}:{exc.code or 'error'}"[:200],
            )
        await self._log.record_result(
            idempotency_key=message.idempotency_key, result=result, now=now
        )
        if result.delivered:
            metrics.emit(metrics.Metric.REMINDER_SENT, kind=message.kind.value)
        return result

    # ------------------------------------------------------------- internals
    async def _template_check(self, message: OutboundMessage, *, now: datetime) -> str | None:
        """None when the message may go as composed; a reason string otherwise.

        Free-form outside the window is not "less good" — Meta drops it. So a
        message with no template and a closed window is refused here, where the
        outcome is recorded, rather than accepted and lost.
        """
        if message.template is not None:
            if self._policy.require_approved_template and self._templates:
                if message.template.name not in self._templates:
                    return f"template_not_approved:{message.template.name}"
            return None

        last_inbound = await self._conversations.last_inbound_at(message.conversation_ref)
        if session_window_open(
            last_inbound, now=now, window_hours=self._policy.session_window_hours
        ):
            return None
        return "session_window_closed_and_no_template"

    async def record_delivery_status(self, *, provider_message_id: str, status: str) -> None:
        """Meta delivery/read callbacks. Feeds no-show risk and ops metrics."""
        await self._log.record_status(
            provider_message_id=provider_message_id, status=status, now=self._clock.now()
        )

    async def sends_in_last_day(self, recipient_ref: str) -> int:
        return await self._log.sends_since(
            recipient_ref=recipient_ref, since=self._clock.now() - timedelta(days=1)
        )

"""Capability 036 — Demo-to-Paid Transition.

Order creation, webhook verification, reconciliation, activation and the
onboarding handoff. This is the money path, so every step is written for the
failure case first.

* **Order creation takes an `ApprovedOffer`.** There is no amount parameter.
* **`accept_webhook` verifies, then dedupes, then reconciles.** In that order:
  verification is cheap and sheds forgeries; dedup is durable and sheds
  replays; reconciliation is the expensive semantic check and runs last on the
  small set that survived.
* **Activation is idempotent on a key derived from the order**, so a timeout
  followed by a retry cannot create a second subscription for one payment.
* **A failed activation after a successful payment is never terminal.** It goes
  to a human with the money already taken, which is the only correct answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from demo_command_center.contracts.ports import (
    NxtutorsGatewayPort,
    PaymentPort,
    ProviderError,
)
from demo_command_center.domain.payments import (
    DEFAULT_ORDER_TTL,
    PaymentEvent,
    PaymentOrder,
    PaymentReconciliationError,
    SubscriptionActivation,
)
from demo_command_center.domain.pricing import ApprovedOffer
from demo_command_center.observability.logging import get_logger
from demo_command_center.repositories.ports import CommerceRepository
from demo_command_center.security.signatures import idempotency_key
from demo_command_center.security.urls import CASHFREE_LINK_POLICY, UrlRejected, validate
from demo_command_center.shared.clock import Clock
from demo_command_center.shared.ids import order_ref as new_order_ref

logger = get_logger("capability.paid_transition")

#: Activation retries. Bounded — an endlessly retrying activation against a
#: broken gateway is indistinguishable from a working one to everything except
#: the customer, who is waiting.
MAX_ACTIVATION_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class OrderResult:
    order: PaymentOrder | None = None
    payment_link: str = ""
    failed: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """The outcome of one webhook. Every branch is a distinct, alarmed metric."""

    accepted: bool = False
    duplicate: bool = False
    order: PaymentOrder | None = None
    reason: str = ""

    @property
    def should_activate(self) -> bool:
        return self.accepted and not self.duplicate and self.order is not None


@dataclass(frozen=True, slots=True)
class ActivationResult:
    activation: SubscriptionActivation
    #: True when a human must intervene: money taken, plan not active.
    needs_human: bool = False


class PaidTransitionCapability:
    def __init__(
        self,
        *,
        payments: PaymentPort,
        gateway: NxtutorsGatewayPort,
        commerce: CommerceRepository,
        clock: Clock,
        return_url: str = "",
    ) -> None:
        self._payments = payments
        self._gateway = gateway
        self._commerce = commerce
        self._clock = clock
        self._return_url = return_url

    # ----------------------------------------------------------- order
    async def create_order(
        self, offer: ApprovedOffer, *, ttl: timedelta = DEFAULT_ORDER_TTL
    ) -> OrderResult:
        """Create the provider order and return the hosted link.

        The link is validated against `CASHFREE_LINK_POLICY` before it can be
        sent. A provider that returns an unexpected host is a redirect we would
        otherwise put in front of a customer about to type card details.
        """
        now = self._clock.now()
        existing = await self._commerce.order_for_conversation(offer.conversation_ref)
        if existing is not None and not existing.settled and not existing.expired(now=now):
            # Reuse rather than create a second order. Two live links for one
            # conversation means two chances to pay for the same thing.
            return OrderResult(order=existing, payment_link=existing.payment_link)

        order = PaymentOrder.from_offer(offer, order_ref=new_order_ref(now=now), now=now, ttl=ttl)
        try:
            created = await self._payments.create_order(
                order=order, offer=offer, return_url=self._return_url
            )
        except ProviderError as exc:
            logger.warning("payment order creation failed", extra={"dcc_code": exc.code})
            return OrderResult(failed=True, reason=f"provider_error:{exc.code or 'unknown'}")

        raw_link = str(created.get("payment_link") or "")
        try:
            link = validate(raw_link, CASHFREE_LINK_POLICY)
        except UrlRejected as exc:
            logger.error("payment link rejected by url policy", extra={"dcc_reason": exc.reason})
            return OrderResult(failed=True, reason="unsafe_payment_link")

        stored = order.model_copy(
            update={
                "provider_order_id": str(created.get("provider_order_id") or ""),
                "payment_link": link,
                "status": order.status,
            }
        )
        await self._commerce.save_order(stored)
        return OrderResult(order=stored, payment_link=link)

    # --------------------------------------------------------- webhook
    async def accept_webhook(self, event: PaymentEvent) -> WebhookResult:
        """Dedupe, then reconcile. The event must already be signature-verified."""
        now = self._clock.now()
        if not event.signature_verified:
            # Defence in depth. The handler verifies; this refuses to be the
            # place where a future caller forgets to.
            return WebhookResult(reason="unverified_event")

        first_time = await self._commerce.record_event(event, now=now)
        if not first_time:
            return WebhookResult(accepted=True, duplicate=True, reason="duplicate_event")

        order = await self._commerce.load_order(event.order_ref)
        try:
            event.reconcile(order, now=now)
        except PaymentReconciliationError as exc:
            logger.error(
                "payment event failed reconciliation",
                extra={"dcc_failure": exc.failure.value},
            )
            return WebhookResult(reason=exc.failure.value)

        assert order is not None  # reconcile() raises ORDER_NOT_FOUND otherwise
        if not event.is_success:
            await self._commerce.save_order(order.model_copy(update={"status": "failed"}))
            return WebhookResult(accepted=True, order=order, reason=event.kind.value)

        paid = order.model_copy(update={"status": "paid", "paid_at": event.occurred_at})
        await self._commerce.save_order(paid)
        return WebhookResult(accepted=True, order=paid)

    # ------------------------------------------------------ activation
    async def activate(self, order: PaymentOrder) -> ActivationResult:
        """Idempotent subscription activation with bounded retries."""
        now = self._clock.now()
        previous = await self._commerce.activation_for(order.order_ref)
        if previous is not None and previous.succeeded:
            return ActivationResult(activation=previous)

        attempt = (previous.attempt + 1) if previous else 1
        # Derived from the order, not from the attempt: every retry must present
        # the *same* key, or the gateway's idempotency does nothing.
        key = idempotency_key("activation", order.order_ref, order.conversation_ref)

        try:
            response = await self._gateway.activate_subscription(
                order_ref=order.order_ref,
                student_ref=order.student_ref,
                idempotency_key=key,
            )
        except ProviderError as exc:
            activation = SubscriptionActivation(
                order_ref=order.order_ref,
                conversation_ref=order.conversation_ref,
                idempotency_key=key,
                attempt=attempt,
                succeeded=False,
                error_code=(exc.code or type(exc).__name__)[:64],
                attempted_at=now,
            )
            await self._commerce.save_activation(activation)
            exhausted = attempt >= MAX_ACTIVATION_ATTEMPTS or not exc.retryable
            if exhausted:
                logger.error(
                    "activation exhausted with payment taken",
                    extra={"dcc_attempt": str(attempt), "dcc_code": exc.code},
                )
            return ActivationResult(activation=activation, needs_human=exhausted)

        subscription_ref = str(response.get("subscription_ref") or "")
        if not subscription_ref:
            activation = SubscriptionActivation(
                order_ref=order.order_ref,
                conversation_ref=order.conversation_ref,
                idempotency_key=key,
                attempt=attempt,
                succeeded=False,
                error_code="no_subscription_ref",
                attempted_at=now,
            )
            await self._commerce.save_activation(activation)
            return ActivationResult(activation=activation, needs_human=True)

        activation = SubscriptionActivation(
            order_ref=order.order_ref,
            conversation_ref=order.conversation_ref,
            idempotency_key=key,
            attempt=attempt,
            succeeded=True,
            subscription_ref=subscription_ref,
            attempted_at=now,
        )
        await self._commerce.save_activation(activation)
        return ActivationResult(activation=activation)

    @staticmethod
    def onboarding_idempotency_key(order: PaymentOrder, subscription_ref: str) -> str:
        """One handoff per subscription, forever.

        Keyed on the subscription rather than the conversation: a customer who
        buys a second plan later is a second, legitimate handoff.
        """
        return idempotency_key("onboarding", order.order_ref, subscription_ref)

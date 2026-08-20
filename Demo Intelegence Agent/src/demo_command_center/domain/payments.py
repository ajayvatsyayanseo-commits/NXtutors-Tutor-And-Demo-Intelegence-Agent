"""Payment orders and verified payment events.

The money path's whole safety story is in two places:

* `PaymentOrder.from_offer()` — the only constructor. It takes an
  `ApprovedOffer`, so an order can never carry an amount that did not come out
  of the deterministic discount engine.
* `PaymentEvent.reconcile()` — checks the provider's report against *our* order
  on three axes (order ref, currency, amount) before anything is treated as
  paid. A signature proves the message came from Cashfree; it does not prove the
  message is about the order we think it is.

Nothing here trusts a customer's claim. `PAID` arrives only from a verified
server-to-server webhook or an explicit reconciliation query against the
provider — never from WhatsApp text, and never from the browser return URL,
which is attacker-controlled.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import SCHEMA_VERSION, StudentRef
from demo_command_center.domain.pricing import ApprovedOffer
from demo_command_center.shared.clock import ensure_utc
from demo_command_center.shared.money import Money

#: How long a hosted payment link stays valid.
DEFAULT_ORDER_TTL = timedelta(hours=48)


class OrderStatus(StrEnum):
    CREATED = "created"
    LINK_SENT = "link_sent"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentEventKind(StrEnum):
    SUCCESS = "payment_success"
    FAILED = "payment_failed"
    USER_DROPPED = "payment_user_dropped"
    REFUND = "refund"
    UNKNOWN = "unknown"


class ReconciliationFailure(StrEnum):
    """Why a verified event was still refused. Each is a distinct alarm."""

    ORDER_NOT_FOUND = "order_not_found"
    ORDER_MISMATCH = "order_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    ORDER_ALREADY_PAID = "order_already_paid"
    ORDER_EXPIRED = "order_expired"


class PaymentReconciliationError(Exception):
    def __init__(self, failure: ReconciliationFailure, detail: str = "") -> None:
        super().__init__(f"{failure.value}{f': {detail}' if detail else ''}")
        self.failure = failure


class PaymentOrder(BaseModel):
    """An order we created with the provider. Built only from an approved offer."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    #: Our reference, unique forever. Reusing one across attempts is how a
    #: second payment gets matched to the first order.
    order_ref: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    demo_id: str = Field(max_length=64)
    student_ref: StudentRef | None = None

    amount_minor: int = Field(ge=1)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    status: OrderStatus = OrderStatus.CREATED

    #: The provider's own id, once it has answered.
    provider_order_id: str = Field(default="", max_length=128)
    #: Hosted page. Validated against `CASHFREE_LINK_POLICY` before it is sent.
    payment_link: str = Field(default="", max_length=1024)

    #: What authorised this amount. Stored so an auditor never has to infer it.
    offer_policy_stamp: str = Field(max_length=96)
    discount_percent: int = Field(default=0, ge=0, le=100)

    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None = None

    @model_validator(mode="after")
    def _utc(self) -> Self:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))
        if self.expires_at <= self.created_at:
            raise ValueError("order must expire after it is created")
        return self

    @classmethod
    def from_offer(
        cls,
        offer: ApprovedOffer,
        *,
        order_ref: str,
        now: datetime,
        ttl: timedelta = DEFAULT_ORDER_TTL,
    ) -> PaymentOrder:
        """The only constructor. Note it takes `ApprovedOffer`, not an int."""
        if not offer.live(now=now):
            raise ValueError("cannot create an order from an expired offer")
        return cls(
            order_ref=order_ref,
            conversation_ref=offer.conversation_ref,
            demo_id=offer.demo_id,
            student_ref=offer.student_ref,
            amount_minor=offer.amount_minor,
            currency=offer.currency,
            offer_policy_stamp=offer.policy_stamp,
            discount_percent=offer.discount_percent,
            created_at=now,
            expires_at=now + ttl,
        )

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    @property
    def settled(self) -> bool:
        return self.status in (OrderStatus.PAID, OrderStatus.CANCELLED)

    def expired(self, *, now: datetime) -> bool:
        return ensure_utc(now) >= self.expires_at


class PaymentEvent(BaseModel):
    """A provider event that has already passed signature verification.

    `signature_verified` cannot be defaulted to True — the verifier sets it, and
    `reconcile()` refuses without it. That makes "we processed an unverified
    webhook" impossible to reach by forgetting a call rather than by writing one.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    #: The provider's event id. The dedup unique index is on this.
    provider_event_id: str = Field(max_length=128)
    kind: PaymentEventKind
    order_ref: str = Field(max_length=64)
    provider_order_id: str = Field(default="", max_length=128)
    amount_minor: int = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    provider_reference: str = Field(default="", max_length=128)
    occurred_at: datetime
    signature_verified: bool = False
    raw_digest: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _utc(self) -> Self:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        return self

    def reconcile(self, order: PaymentOrder | None, *, now: datetime) -> None:
        """Raise unless this event genuinely settles `order`.

        Deliberately does not return a bool: a caller that forgets to check a
        boolean has a security bug, whereas a caller that forgets to catch an
        exception has an outage. The failure mode should be the loud one.
        """
        if not self.signature_verified:
            raise PaymentReconciliationError(
                ReconciliationFailure.ORDER_MISMATCH, "event was never signature-verified"
            )
        if order is None:
            raise PaymentReconciliationError(ReconciliationFailure.ORDER_NOT_FOUND, self.order_ref)
        if order.order_ref != self.order_ref:
            raise PaymentReconciliationError(ReconciliationFailure.ORDER_MISMATCH)
        if order.currency != self.currency:
            raise PaymentReconciliationError(
                ReconciliationFailure.CURRENCY_MISMATCH, f"{order.currency} vs {self.currency}"
            )
        if order.amount_minor != self.amount_minor:
            # Exact equality. Not "at least" — an overpayment is a data problem
            # that a human must look at, not a reason to activate a plan.
            raise PaymentReconciliationError(
                ReconciliationFailure.AMOUNT_MISMATCH,
                f"expected {order.amount_minor}, received {self.amount_minor}",
            )
        if order.status is OrderStatus.PAID:
            raise PaymentReconciliationError(ReconciliationFailure.ORDER_ALREADY_PAID)
        if order.expired(now=now) and self.kind is PaymentEventKind.SUCCESS:
            # Worth flagging rather than silently accepting: a payment against
            # an expired link means the link outlived our record of it.
            raise PaymentReconciliationError(ReconciliationFailure.ORDER_EXPIRED)

    @property
    def is_success(self) -> bool:
        return self.kind is PaymentEventKind.SUCCESS and self.signature_verified


class SubscriptionActivation(BaseModel):
    """One attempt to turn a verified payment into an active subscription."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    order_ref: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    #: Deterministic. The gateway's idempotency key, so a retry after a timeout
    #: cannot create a second subscription for one payment.
    idempotency_key: str = Field(max_length=128)
    attempt: int = Field(default=1, ge=1)
    succeeded: bool = False
    subscription_ref: str = Field(default="", max_length=128)
    error_code: str = Field(default="", max_length=64)
    attempted_at: datetime

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        object.__setattr__(self, "attempted_at", ensure_utc(self.attempted_at))
        if self.succeeded and not self.subscription_ref:
            raise ValueError("a successful activation must carry a subscription_ref")
        return self

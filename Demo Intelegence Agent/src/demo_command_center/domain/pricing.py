"""Plan quotes, discount decisions and the approved offer.

The type system carries the authorization here. There is no way to build a
`PaymentOrder` (see `domain/payments.py`) except from an `ApprovedOffer`, and
the only way to build an `ApprovedOffer` is
`DiscountDecision.approve()` — which refuses unless the decision's status is
`APPROVED`. A model can produce text; it cannot produce one of these.

`PlanQuote` is authoritative and comes from the NXTutors gateway. It is never
computed here and never remembered across a conversation for longer than its
TTL: quoting yesterday's price and charging today's is a chargeback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import SCHEMA_VERSION, PlanRef, StudentRef
from demo_command_center.domain.objections import ObjectionCategory
from demo_command_center.shared.clock import ensure_utc
from demo_command_center.shared.money import Money

#: A quote older than this is re-fetched rather than reused.
QUOTE_TTL = timedelta(minutes=30)


class DiscountStatus(StrEnum):
    APPROVED = "approved"
    #: Above the auto-approval ceiling. Waiting on a human.
    ESCALATED = "escalated"
    DENIED = "denied"
    #: Policy allows nothing here — no qualifying objection, or abuse detected.
    NOT_APPLICABLE = "not_applicable"


class DenialReason(StrEnum):
    NO_QUALIFYING_OBJECTION = "no_qualifying_objection"
    PRICE_FLOOR_BREACHED = "price_floor_breached"
    OFFER_LIMIT_REACHED = "offer_limit_reached"
    REPEAT_REQUESTS = "repeat_requests"
    DEMO_NOT_COMPLETED = "demo_not_completed"
    POLICY_DISABLED = "policy_disabled"
    NONE = "none"


class PlanQuote(BaseModel):
    """An authoritative price from the NXTutors gateway. Never computed here."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    plan_ref: PlanRef
    plan_name: str = Field(max_length=120)
    list_price_minor: int = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    billing_period: str = Field(default="monthly", max_length=24)
    #: Sessions, hours or whatever the plan actually sells. Rendered verbatim.
    inclusions: tuple[str, ...] = ()
    fetched_at: datetime

    @model_validator(mode="after")
    def _utc(self) -> Self:
        object.__setattr__(self, "fetched_at", ensure_utc(self.fetched_at))
        return self

    @property
    def list_price(self) -> Money:
        return Money(self.list_price_minor, self.currency)

    def fresh(self, *, now: datetime, ttl: timedelta = QUOTE_TTL) -> bool:
        return ensure_utc(now) - self.fetched_at <= ttl


class DiscountDecision(BaseModel):
    """The engine's verdict. Deterministic, policy-versioned, auditable.

    Every field an auditor would ask for six months later is here: the band, the
    exact percentage, the resulting amount, the floor it was checked against, and
    the checksummed policy that produced all of it.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    conversation_ref: str = Field(max_length=128)
    demo_id: str = Field(max_length=64)
    student_ref: StudentRef | None = None

    status: DiscountStatus
    band_name: str = Field(default="", max_length=48)
    percent: int = Field(default=0, ge=0, le=100)
    list_price_minor: int = Field(ge=0)
    discount_minor: int = Field(default=0, ge=0)
    payable_minor: int = Field(ge=0)
    floor_minor: int = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)

    triggers: tuple[ObjectionCategory, ...] = ()
    conditions: tuple[str, ...] = ()
    reason_code: DenialReason = DenialReason.NONE
    requires_human_approval: bool = False
    approved_by: str = Field(default="", max_length=64)

    #: `discount@v1#a1b2c3d4e5f6`. The bytes that produced this decision.
    policy_stamp: str = Field(max_length=96)
    valid_until: datetime | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def _arithmetic_is_consistent(self) -> Self:
        """The numbers must add up and must not breach the floor.

        Checked in the model rather than only in the engine because this object
        crosses a process boundary (it is persisted and re-read), and a row that
        was correct when written must still be correct when it is used to build
        a payment order.
        """
        object.__setattr__(self, "decided_at", ensure_utc(self.decided_at))
        if self.list_price_minor - self.discount_minor != self.payable_minor:
            raise ValueError(
                f"discount arithmetic does not balance: "
                f"{self.list_price_minor} - {self.discount_minor} != {self.payable_minor}"
            )
        if self.status is DiscountStatus.APPROVED and self.payable_minor < self.floor_minor:
            raise ValueError(
                f"approved payable {self.payable_minor} is below the price floor {self.floor_minor}"
            )
        if self.status is DiscountStatus.APPROVED and self.requires_human_approval:
            if not self.approved_by:
                raise ValueError("a decision needing human approval cannot be APPROVED unsigned")
        if self.percent > 0 and self.status is DiscountStatus.NOT_APPLICABLE:
            raise ValueError("NOT_APPLICABLE cannot carry a non-zero percent")
        return self

    @property
    def payable(self) -> Money:
        return Money(self.payable_minor, self.currency)

    @property
    def discount(self) -> Money:
        return Money(self.discount_minor, self.currency)

    def live(self, *, now: datetime) -> bool:
        return self.valid_until is None or ensure_utc(now) < self.valid_until

    def approve(self, *, now: datetime) -> ApprovedOffer:
        """The only route to something a payment order can be built from.

        `ESCALATED` is the one status that cannot produce an offer: a human has
        not decided yet, so there is no authorised price.

        Every other status can. `DENIED` and `NOT_APPLICABLE` both mean "no
        discount", not "no sale" — the payable amount is the list price, which
        the validator above has already checked against the floor. Refusing
        those was a real bug: a customer with no qualifying objection could
        never be sent a payment link at all.
        """
        if self.status is DiscountStatus.ESCALATED:
            raise ValueError("cannot build an offer while a discount awaits human approval")
        if self.payable_minor <= 0:
            raise ValueError("cannot build an offer for a zero amount")
        if not self.live(now=now):
            raise ValueError("offer has expired")
        return ApprovedOffer(
            conversation_ref=self.conversation_ref,
            demo_id=self.demo_id,
            student_ref=self.student_ref,
            amount_minor=self.payable_minor,
            currency=self.currency,
            list_price_minor=self.list_price_minor,
            discount_percent=self.percent,
            band_name=self.band_name,
            policy_stamp=self.policy_stamp,
            valid_until=self.valid_until,
            approved_at=ensure_utc(now),
        )


class ApprovedOffer(BaseModel):
    """A price a customer may actually be charged.

    Constructed only by `DiscountDecision.approve()`. The payment layer accepts
    nothing else, which is how "the LLM never chooses the amount" becomes a
    property of the type graph rather than a rule in a prompt.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    conversation_ref: str = Field(max_length=128)
    demo_id: str = Field(max_length=64)
    student_ref: StudentRef | None = None
    amount_minor: int = Field(ge=1)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    list_price_minor: int = Field(ge=0)
    discount_percent: int = Field(ge=0, le=100)
    band_name: str = Field(default="", max_length=48)
    policy_stamp: str = Field(max_length=96)
    valid_until: datetime | None = None
    approved_at: datetime

    @property
    def amount(self) -> Money:
        return Money(self.amount_minor, self.currency)

    def live(self, *, now: datetime) -> bool:
        return self.valid_until is None or ensure_utc(now) < self.valid_until

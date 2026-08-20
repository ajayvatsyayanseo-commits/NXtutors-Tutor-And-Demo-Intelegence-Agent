"""The money path: forged, replayed, mismatched and expired payment events.

Every test here drives the *real* verifier over *real* raw bytes minted by the
fake, so a change to the signing scheme fails these rather than passing a mock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from demo_command_center.capabilities.paid_transition.service import PaidTransitionCapability
from demo_command_center.domain.payments import (
    PaymentEvent,
    PaymentEventKind,
    PaymentOrder,
    PaymentReconciliationError,
    ReconciliationFailure,
)
from demo_command_center.domain.pricing import (
    DiscountDecision,
    DiscountStatus,
    PlanQuote,
)
from demo_command_center.integrations.fakes import FakeCashfree, FakeGateway
from demo_command_center.security.signatures import (
    SignatureError,
    SignatureFailure,
    cashfree_signature,
    verify_cashfree,
)
from demo_command_center.shared.clock import FrozenClock
from demo_command_center.storage.memory.commerce import InMemoryCommerceRepository

pytestmark = pytest.mark.security

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
AMOUNT = 480_000
SECRET = "fixture-cashfree-secret"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def offer():  # type: ignore[no-untyped-def]
    decision = DiscountDecision(
        conversation_ref="cv_pay",
        demo_id="dmo_pay",
        student_ref="stu_pay",
        status=DiscountStatus.APPROVED,
        percent=0,
        list_price_minor=AMOUNT,
        discount_minor=0,
        payable_minor=AMOUNT,
        floor_minor=int(AMOUNT * 0.7),
        policy_stamp="discount@v1#test",
        decided_at=NOW,
    )
    return decision.approve(now=NOW)


@pytest.fixture
def capability(
    clock: FrozenClock,
) -> tuple[PaidTransitionCapability, InMemoryCommerceRepository, FakeCashfree]:
    commerce = InMemoryCommerceRepository()
    payments = FakeCashfree(clock=clock, secret_key=SECRET)
    return (
        PaidTransitionCapability(
            payments=payments, gateway=FakeGateway(clock=clock), commerce=commerce, clock=clock
        ),
        commerce,
        payments,
    )


def event_for(order: PaymentOrder, **overrides: object) -> PaymentEvent:
    base: dict[str, object] = {
        "provider_event_id": "cf_evt_1",
        "kind": PaymentEventKind.SUCCESS,
        "order_ref": order.order_ref,
        "amount_minor": order.amount_minor,
        "currency": order.currency,
        "occurred_at": NOW,
        "signature_verified": True,
    }
    base.update(overrides)
    return PaymentEvent.model_validate(base)


# ------------------------------------------------------ signature verification


def test_a_forged_signature_is_rejected() -> None:
    body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'
    timestamp = str(int(NOW.timestamp()))
    with pytest.raises(SignatureError) as exc:
        verify_cashfree(
            secret_key=SECRET,
            raw_body=body,
            timestamp=timestamp,
            provided="not-the-signature",
            max_body_bytes=65_536,
            tolerance_seconds=300,
            now=NOW.timestamp(),
        )
    assert exc.value.reason is SignatureFailure.SIGNATURE_MISMATCH


def test_a_valid_signature_over_the_exact_bytes_is_accepted() -> None:
    body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK","data":{}}'
    timestamp = str(int(NOW.timestamp()))
    verify_cashfree(
        secret_key=SECRET,
        raw_body=body,
        timestamp=timestamp,
        provided=cashfree_signature(SECRET, timestamp=timestamp, raw_body=body),
        max_body_bytes=65_536,
        tolerance_seconds=300,
        now=NOW.timestamp(),
    )


def test_a_signature_for_different_bytes_does_not_verify() -> None:
    """Re-serialising the parsed JSON is the classic way to break this."""
    signed = b'{"a":1,"b":2}'
    arrived = b'{"b":2,"a":1}'  # same object, different bytes
    timestamp = str(int(NOW.timestamp()))
    with pytest.raises(SignatureError):
        verify_cashfree(
            secret_key=SECRET,
            raw_body=arrived,
            timestamp=timestamp,
            provided=cashfree_signature(SECRET, timestamp=timestamp, raw_body=signed),
            max_body_bytes=65_536,
            tolerance_seconds=300,
            now=NOW.timestamp(),
        )


def test_a_replayed_webhook_outside_the_window_is_rejected() -> None:
    body = b"{}"
    old = NOW - timedelta(hours=2)
    timestamp = str(int(old.timestamp()))
    with pytest.raises(SignatureError) as exc:
        verify_cashfree(
            secret_key=SECRET,
            raw_body=body,
            timestamp=timestamp,
            provided=cashfree_signature(SECRET, timestamp=timestamp, raw_body=body),
            max_body_bytes=65_536,
            tolerance_seconds=300,
            now=NOW.timestamp(),
        )
    assert exc.value.reason is SignatureFailure.TIMESTAMP_OUT_OF_WINDOW


def test_an_oversized_body_is_rejected_before_any_hashing() -> None:
    with pytest.raises(SignatureError) as exc:
        verify_cashfree(
            secret_key=SECRET,
            raw_body=b"x" * 200_000,
            timestamp=str(int(NOW.timestamp())),
            provided="whatever",
            max_body_bytes=65_536,
            tolerance_seconds=300,
            now=NOW.timestamp(),
        )
    assert exc.value.reason is SignatureFailure.BODY_TOO_LARGE


def test_the_fake_mints_webhooks_the_real_verifier_accepts() -> None:
    """Proves the E2E payment path exercises production verification code."""
    fake = FakeCashfree(clock=FrozenClock(NOW), secret_key=SECRET)
    raw, timestamp, signature = fake.webhook(order_ref="nxo_1", amount_minor=AMOUNT, now=NOW)
    verify_cashfree(
        secret_key=SECRET,
        raw_body=raw,
        timestamp=timestamp,
        provided=signature,
        max_body_bytes=65_536,
        tolerance_seconds=300,
        now=NOW.timestamp(),
    )


# ------------------------------------------------------------ reconciliation


async def test_an_unverified_event_never_reaches_reconciliation(capability, offer) -> None:  # type: ignore[no-untyped-def]
    paid, commerce, _ = capability
    created = await paid.create_order(offer)
    assert created.order is not None
    result = await paid.accept_webhook(event_for(created.order, signature_verified=False))
    assert not result.accepted
    assert result.reason == "unverified_event"


async def test_a_duplicate_provider_event_is_ignored(capability, offer) -> None:  # type: ignore[no-untyped-def]
    """The durable replay defence, independent of the signature check."""
    paid, _, _ = capability
    created = await paid.create_order(offer)
    assert created.order is not None
    first = await paid.accept_webhook(event_for(created.order))
    second = await paid.accept_webhook(event_for(created.order))
    assert first.should_activate
    assert second.duplicate
    assert not second.should_activate


async def test_an_amount_mismatch_is_refused(capability, offer) -> None:  # type: ignore[no-untyped-def]
    paid, _, _ = capability
    created = await paid.create_order(offer)
    assert created.order is not None
    result = await paid.accept_webhook(
        event_for(created.order, amount_minor=1, provider_event_id="cf_evt_wrong")
    )
    assert result.reason == ReconciliationFailure.AMOUNT_MISMATCH.value
    assert not result.should_activate


async def test_an_overpayment_is_also_refused(capability, offer) -> None:  # type: ignore[no-untyped-def]
    """Exact equality. An overpayment is a data problem for a human."""
    paid, _, _ = capability
    created = await paid.create_order(offer)
    assert created.order is not None
    result = await paid.accept_webhook(
        event_for(created.order, amount_minor=AMOUNT + 100, provider_event_id="cf_evt_over")
    )
    assert result.reason == ReconciliationFailure.AMOUNT_MISMATCH.value


async def test_a_currency_mismatch_is_refused(capability, offer) -> None:  # type: ignore[no-untyped-def]
    paid, _, _ = capability
    created = await paid.create_order(offer)
    assert created.order is not None
    result = await paid.accept_webhook(
        event_for(created.order, currency="USD", provider_event_id="cf_evt_usd")
    )
    assert result.reason == ReconciliationFailure.CURRENCY_MISMATCH.value


async def test_an_event_for_an_unknown_order_is_refused(capability) -> None:  # type: ignore[no-untyped-def]
    paid, _, _ = capability
    orphan = PaymentEvent(
        provider_event_id="cf_evt_orphan",
        kind=PaymentEventKind.SUCCESS,
        order_ref="nxo_does_not_exist",
        amount_minor=AMOUNT,
        occurred_at=NOW,
        signature_verified=True,
    )
    result = await paid.accept_webhook(orphan)
    assert result.reason == ReconciliationFailure.ORDER_NOT_FOUND.value


def test_reconcile_raises_rather_than_returning_a_boolean() -> None:
    """A caller who forgets a bool has a security bug; one who forgets a
    try/except has an outage. The loud failure is the right one."""
    order = PaymentOrder(
        order_ref="nxo_1",
        conversation_ref="cv",
        demo_id="dmo",
        amount_minor=AMOUNT,
        offer_policy_stamp="s",
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(PaymentReconciliationError):
        event_for(order, amount_minor=1).reconcile(order, now=NOW)


# ---------------------------------------------------------- amount authority


def test_a_payment_order_cannot_be_built_from_a_bare_amount() -> None:
    """`from_offer` is the only constructor, and it takes an `ApprovedOffer`."""
    import inspect

    signature = inspect.signature(PaymentOrder.from_offer)
    assert "offer" in signature.parameters
    assert "amount" not in signature.parameters
    assert "amount_minor" not in signature.parameters


def test_an_escalated_discount_cannot_produce_an_offer() -> None:
    """A price awaiting human approval is not an authorised price."""
    decision = DiscountDecision(
        conversation_ref="cv",
        demo_id="dmo",
        status=DiscountStatus.ESCALATED,
        percent=15,
        list_price_minor=AMOUNT,
        discount_minor=72_000,
        payable_minor=408_000,
        floor_minor=336_000,
        policy_stamp="discount@v1#test",
        decided_at=NOW,
    )
    with pytest.raises(ValueError, match="awaits human approval"):
        decision.approve(now=NOW)


def test_a_decision_whose_arithmetic_does_not_balance_cannot_exist() -> None:
    with pytest.raises(ValueError, match="does not balance"):
        DiscountDecision(
            conversation_ref="cv",
            demo_id="dmo",
            status=DiscountStatus.APPROVED,
            percent=10,
            list_price_minor=AMOUNT,
            discount_minor=1,
            payable_minor=AMOUNT,  # should be AMOUNT - 1
            floor_minor=0,
            policy_stamp="s",
            decided_at=NOW,
        )


def test_an_approved_decision_below_the_price_floor_cannot_exist() -> None:
    with pytest.raises(ValueError, match="below the price floor"):
        DiscountDecision(
            conversation_ref="cv",
            demo_id="dmo",
            status=DiscountStatus.APPROVED,
            percent=50,
            list_price_minor=AMOUNT,
            discount_minor=240_000,
            payable_minor=240_000,
            floor_minor=336_000,
            policy_stamp="s",
            decided_at=NOW,
        )


# --------------------------------------------------------------- activation


async def test_activation_retries_reuse_one_idempotency_key(clock: FrozenClock, offer) -> None:  # type: ignore[no-untyped-def]
    """A retry must present the same key, or the gateway dedupe does nothing."""
    commerce = InMemoryCommerceRepository()
    gateway = FakeGateway(clock=clock, activation_fails_before_attempt=2)
    paid = PaidTransitionCapability(
        payments=FakeCashfree(clock=clock, secret_key=SECRET),
        gateway=gateway,
        commerce=commerce,
        clock=clock,
    )
    created = await paid.create_order(offer)
    assert created.order is not None

    first = await paid.activate(created.order)
    second = await paid.activate(created.order)
    third = await paid.activate(created.order)

    assert not first.activation.succeeded
    assert not second.activation.succeeded
    assert third.activation.succeeded
    assert len(set(gateway.activation_calls)) == 1, "every attempt must reuse one key"


async def test_a_successful_activation_is_never_repeated(clock: FrozenClock, offer) -> None:  # type: ignore[no-untyped-def]
    commerce = InMemoryCommerceRepository()
    gateway = FakeGateway(clock=clock)
    paid = PaidTransitionCapability(
        payments=FakeCashfree(clock=clock, secret_key=SECRET),
        gateway=gateway,
        commerce=commerce,
        clock=clock,
    )
    created = await paid.create_order(offer)
    assert created.order is not None
    first = await paid.activate(created.order)
    second = await paid.activate(created.order)
    assert first.activation.subscription_ref == second.activation.subscription_ref
    assert len(gateway.activation_calls) == 1, "the second call must short-circuit"


async def test_exhausted_activation_after_payment_needs_a_human(clock: FrozenClock, offer) -> None:  # type: ignore[no-untyped-def]
    """Money taken, plan not active. Never terminal, never silent."""
    commerce = InMemoryCommerceRepository()
    paid = PaidTransitionCapability(
        payments=FakeCashfree(clock=clock, secret_key=SECRET),
        gateway=FakeGateway(clock=clock, fail_activation=True),
        commerce=commerce,
        clock=clock,
    )
    created = await paid.create_order(offer)
    assert created.order is not None
    for _ in range(3):
        result = await paid.activate(created.order)
    assert result.needs_human


async def test_one_live_order_per_conversation(capability, offer) -> None:  # type: ignore[no-untyped-def]
    """Two live links means two chances to pay for the same thing."""
    paid, _, _ = capability
    first = await paid.create_order(offer)
    second = await paid.create_order(offer)
    assert first.order is not None and second.order is not None
    assert first.order.order_ref == second.order.order_ref


async def test_the_payment_link_must_be_a_cashfree_host(clock: FrozenClock, offer) -> None:  # type: ignore[no-untyped-def]
    """A provider returning an unexpected host is a redirect we would put in
    front of a customer about to type card details."""
    payments = FakeCashfree(clock=clock, secret_key=SECRET)

    async def hostile(*, order, offer, return_url):  # type: ignore[no-untyped-def]
        return {
            "provider_order_id": "x",
            "payment_link": "https://evil.test/pay",
            "order_status": "ACTIVE",
        }

    payments.create_order = hostile  # type: ignore[method-assign]
    paid = PaidTransitionCapability(
        payments=payments,
        gateway=FakeGateway(clock=clock),
        commerce=InMemoryCommerceRepository(),
        clock=clock,
    )
    result = await paid.create_order(offer)
    assert result.failed
    assert result.reason == "unsafe_payment_link"


def test_plan_quotes_never_round_trip_through_a_float() -> None:
    """`0.1 + 0.2 != 0.3` turns webhook verification into an intermittent fail."""
    quote = PlanQuote(
        plan_ref="plan_monthly",
        plan_name="Monthly",
        list_price_minor=479_999,
        fetched_at=NOW,
    )
    assert quote.list_price.minor == 479_999
    assert str(quote.list_price.major) == "4799.99"

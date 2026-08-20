"""Deterministic fakes for every remaining provider.

These back `make demo`, the E2E suite and `dcc-doctor`. They are test doubles
with real behaviour, not stubs that return `True`:

* `FakeCalendar` enforces its own double-booking rule, so the concurrency test
  is meaningful.
* `FakeCashfree` produces webhook payloads that pass the *real* signature
  verifier, so `tests/security/test_payment_webhook.py` exercises the production
  code path rather than a mock of it.
* `FakeWhatsApp` records what it was asked to send and never touches a network.

Everything here is opt-in failure: nothing fails unless a test asks it to,
because a fake that fails randomly makes a red suite meaningless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from demo_command_center.contracts.ports import ProviderRejected, ProviderUnavailable
from demo_command_center.domain.messages import OutboundMessage, SendOutcome, SendResult
from demo_command_center.domain.payments import PaymentOrder
from demo_command_center.domain.pricing import ApprovedOffer, PlanQuote
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.security.signatures import cashfree_signature
from demo_command_center.shared.clock import Clock, SystemClock

PROVIDER_GATEWAY = "nxtutors_gateway"
PROVIDER_CALENDAR = "google_calendar"
PROVIDER_PAYMENTS = "cashfree"
PROVIDER_WHATSAPP = "meta_whatsapp"


# ------------------------------------------------------------------ gateway


@dataclass
class FakeGateway:
    """NXTutors website gateway double.

    Availability is generated rather than fixed: a test that books "tomorrow at
    6pm" needs a slot to exist tomorrow, and a hard-coded fixture date makes the
    suite start failing on a particular Tuesday.
    """

    clock: Clock = field(default_factory=SystemClock)
    fail_identity: bool = False
    fail_activation: bool = False
    activation_fails_before_attempt: int = 0
    list_price_minor: int = 480_000
    authorised_regions: list[str] = field(default_factory=lambda: ["north", "south"])
    tutor_email: str = "tutor@fixture.invalid"
    activation_calls: list[str] = field(default_factory=list)
    _activation_attempts: int = 0
    _subscriptions: dict[str, str] = field(default_factory=dict)

    async def resolve_identity(self, *, phone_hash: str, conversation_ref: str) -> dict[str, Any]:
        if self.fail_identity:
            raise ProviderUnavailable(PROVIDER_GATEWAY, "identity service down")
        return {"student_ref": f"stu_{phone_hash[:12]}", "known_customer": False}

    async def resolve_tutor_contacts(self, *, tutor_ref: str) -> dict[str, Any]:
        return {
            "tutor_ref": tutor_ref,
            "email": self.tutor_email,
            "whatsapp_ref": f"wa_{tutor_ref}",
        }

    async def tutor_availability(
        self, *, tutor_ref: str, from_at: datetime, to_at: datetime
    ) -> list[TimeSlot]:
        """Weekday evening slots at 16:00, 18:00 and 20:00 IST."""
        slots: list[TimeSlot] = []
        cursor = from_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        for day in range(14):
            date = cursor + timedelta(days=day)
            if date.weekday() >= 5:
                continue
            for hour_ist in (16, 18, 20):
                # 16:00 IST == 10:30 UTC.
                start = date.replace(hour=(hour_ist - 5) % 24, minute=30)
                if start < from_at or start > to_at:
                    continue
                slots.append(
                    TimeSlot(starts_at=start, duration_minutes=45, timezone="Asia/Kolkata")
                )
        return slots

    async def plan_quote(self, *, student_ref: str | None, plan_ref: str | None) -> PlanQuote:
        return PlanQuote(
            plan_ref=plan_ref or "plan_monthly_standard",
            plan_name="Monthly Standard",
            list_price_minor=self.list_price_minor,
            currency="INR",
            billing_period="monthly",
            inclusions=("12 sessions per month", "Progress reports"),
            fetched_at=self.clock.now(),
        )

    async def discount_eligibility(
        self, *, student_ref: str | None, lookback_days: int
    ) -> dict[str, Any]:
        return {"prior_offers": 0, "eligible": True}

    async def record_demo(self, *, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return {"recorded": True, "idempotency_key": idempotency_key}

    async def activate_subscription(
        self, *, order_ref: str, student_ref: str | None, idempotency_key: str
    ) -> dict[str, Any]:
        self.activation_calls.append(idempotency_key)
        # Idempotent by key: a retry returns the same subscription rather than
        # creating a second one. This is the behaviour the real gateway must
        # have, and the test that proves we rely on it.
        if idempotency_key in self._subscriptions:
            return {"subscription_ref": self._subscriptions[idempotency_key], "created": False}

        self._activation_attempts += 1
        if self.fail_activation:
            raise ProviderUnavailable(PROVIDER_GATEWAY, "activation service down")
        if self._activation_attempts <= self.activation_fails_before_attempt:
            raise ProviderUnavailable(PROVIDER_GATEWAY, "transient activation failure")

        subscription_ref = f"sub_{order_ref[-12:]}"
        self._subscriptions[idempotency_key] = subscription_ref
        return {"subscription_ref": subscription_ref, "created": True}

    async def region_authorization(self, *, operator_ref: str) -> list[str]:
        return list(self.authorised_regions)


# ----------------------------------------------------------------- calendar


@dataclass
class FakeCalendar:
    """Google Calendar + Meet double with real double-booking prevention."""

    clock: Clock = field(default_factory=SystemClock)
    fail_create: bool = False
    omit_conference: bool = False
    events: dict[str, dict[str, Any]] = field(default_factory=dict)
    _busy: dict[str, str] = field(default_factory=dict)
    _sequence: int = 0

    async def create_event(
        self,
        *,
        summary: str,
        description: str,
        slot: TimeSlot,
        attendee_emails: tuple[str, ...],
        with_conference: bool,
        conference_request_id: str,
        location: str | None = None,
    ) -> dict[str, Any]:
        if self.fail_create:
            raise ProviderUnavailable(PROVIDER_CALENDAR, "calendar unavailable")

        key = f"{'|'.join(sorted(attendee_emails))}@{slot.starts_at.isoformat()}"
        if key in self._busy:
            raise ProviderRejected(PROVIDER_CALENDAR, "conflict", status_code=409, code="conflict")

        self._sequence += 1
        event_id = f"evt_fixture_{self._sequence:04d}"
        self._busy[key] = event_id
        event = {
            "event_id": event_id,
            "summary": summary,
            "description": description,
            "start": slot.starts_at.isoformat(),
            "end": slot.ends_at.isoformat(),
            "timezone": slot.timezone,
            "attendees": [{"email": email, "role": "tutor"} for email in attendee_emails],
            "location": location,
            # Meet URLs are real-shaped so the URL policy check is exercised.
            "meet_url": ""
            if (not with_conference or self.omit_conference)
            else f"https://meet.google.com/fix-{self._sequence:04d}-abc",
            "conference_request_id": conference_request_id,
        }
        self.events[event_id] = event
        return event

    async def patch_event(
        self, *, event_id: str, slot: TimeSlot | None = None, description: str | None = None
    ) -> dict[str, Any]:
        event = self.events.get(event_id)
        if event is None:
            raise ProviderRejected(
                PROVIDER_CALENDAR, "not found", status_code=404, code="not_found"
            )
        if slot is not None:
            event["start"] = slot.starts_at.isoformat()
            event["end"] = slot.ends_at.isoformat()
        if description is not None:
            event["description"] = description
        return event

    async def cancel_event(self, *, event_id: str) -> None:
        event = self.events.pop(event_id, None)
        if event is not None:
            for key, value in list(self._busy.items()):
                if value == event_id:
                    self._busy.pop(key, None)

    async def get_event(self, *, event_id: str) -> dict[str, Any]:
        event = self.events.get(event_id)
        if event is None:
            raise ProviderRejected(
                PROVIDER_CALENDAR, "not found", status_code=404, code="not_found"
            )
        return event

    def mark_attendance(self, event_id: str, *, participants: int, duration_minutes: int) -> None:
        """Test affordance: record what the conference observed."""
        event = self.events.get(event_id)
        if event is not None:
            event["participant_count"] = participants
            event["duration_minutes"] = duration_minutes


# ----------------------------------------------------------------- payments


@dataclass
class FakeCashfree:
    """Payment double that can mint webhooks the real verifier accepts."""

    clock: Clock = field(default_factory=SystemClock)
    # A fixture value, not a credential. It exists so the fake can mint a
    # webhook the real verifier accepts.
    secret_key: str = "fixture-cashfree-secret"  # noqa: S105
    fail_create: bool = False
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    _sequence: int = 0

    async def create_order(
        self, *, order: PaymentOrder, offer: ApprovedOffer, return_url: str
    ) -> dict[str, Any]:
        if self.fail_create:
            raise ProviderUnavailable(PROVIDER_PAYMENTS, "order service down")
        if order.amount_minor != offer.amount_minor:  # pragma: no cover - type-enforced
            raise ProviderRejected(PROVIDER_PAYMENTS, "amount mismatch", code="amount_mismatch")

        self._sequence += 1
        provider_order_id = f"cf_order_{self._sequence:05d}"
        record = {
            "provider_order_id": provider_order_id,
            "order_ref": order.order_ref,
            "order_amount": order.amount_minor / 100,
            "order_currency": order.currency,
            "payment_link": f"https://payments-test.cashfree.com/pay/{provider_order_id}",
            "order_status": "ACTIVE",
        }
        self.orders[order.order_ref] = record
        return record

    async def fetch_order(self, *, order_ref: str) -> dict[str, Any]:
        record = self.orders.get(order_ref)
        if record is None:
            raise ProviderRejected(
                PROVIDER_PAYMENTS, "not found", status_code=404, code="not_found"
            )
        return record

    def webhook(
        self,
        *,
        order_ref: str,
        amount_minor: int,
        currency: str = "INR",
        event_id: str = "",
        status: str = "SUCCESS",
        now: datetime | None = None,
    ) -> tuple[bytes, str, str]:
        """`(raw_body, timestamp, signature)` — verifiable by the real code.

        Returning raw bytes rather than a dict is the point: the production
        verifier signs over exactly what arrived, and a test that hands it a
        re-serialised dict is testing a different function.
        """
        moment = now or self.clock.now()
        body = {
            "type": "PAYMENT_SUCCESS_WEBHOOK" if status == "SUCCESS" else "PAYMENT_FAILED_WEBHOOK",
            "event_time": moment.isoformat(),
            "data": {
                "order": {
                    "order_id": order_ref,
                    "order_amount": round(amount_minor / 100, 2),
                    "order_currency": currency,
                },
                "payment": {
                    "cf_payment_id": event_id or f"cf_pay_{self._sequence:05d}",
                    "payment_status": status,
                    "payment_amount": round(amount_minor / 100, 2),
                    "payment_currency": currency,
                },
            },
        }
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(moment.timestamp()))
        return (
            raw,
            timestamp,
            cashfree_signature(self.secret_key, timestamp=timestamp, raw_body=raw),
        )


# ----------------------------------------------------------------- whatsapp


@dataclass
class FakeWhatsApp:
    """Records sends. Never opens a socket."""

    fail: bool = False
    sent: list[tuple[OutboundMessage, str]] = field(default_factory=list)
    _sequence: int = 0

    async def send(self, message: OutboundMessage, *, recipient: str) -> SendResult:
        if self.fail:
            raise ProviderUnavailable(PROVIDER_WHATSAPP, "graph api unavailable")
        self._sequence += 1
        self.sent.append((message, recipient))
        return SendResult(
            outcome=SendOutcome.SENT,
            idempotency_key=message.idempotency_key,
            provider_message_id=f"wamid.FIXTURE{self._sequence:05d}",
            sent_at=datetime.now(UTC),
        )

    def bodies(self) -> list[str]:
        return [message.body for message, _ in self.sent]

    def kinds(self) -> list[str]:
        return [message.kind.value for message, _ in self.sent]


@dataclass
class FakeContacts:
    """Recipient-ref resolution and opt-out state."""

    opted_out_refs: set[str] = field(default_factory=set)
    unresolvable: set[str] = field(default_factory=set)

    async def resolve(self, recipient_ref: str) -> str | None:
        if recipient_ref in self.unresolvable:
            return None
        return f"+9199{abs(hash(recipient_ref)) % 100_000_000:08d}"

    async def opted_out(self, recipient_ref: str) -> bool:
        return recipient_ref in self.opted_out_refs


# ---------------------------------------------------------- llm and agents


@dataclass
class FakeLlm:
    """Returns a canned structured response per purpose. Never calls a network.

    `responses` is keyed by purpose so one fake serves classification,
    extraction and objection analysis without a branch in every test.
    """

    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def structured(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append((purpose, user))
        if self.fail:
            raise ProviderUnavailable("openai", "model unavailable")
        return self.responses.get(purpose, {})


@dataclass
class FakeAgentBus:
    dispatched: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    async def dispatch(self, envelope: dict[str, Any], *, url: str) -> dict[str, Any]:
        if self.fail:
            raise ProviderUnavailable("agent_bus", "peer unavailable")
        self.dispatched.append((url, envelope))
        return {"status": "accepted"}


@dataclass
class FakeScheduler:
    """One-shot callbacks. `schedule` replaces by name, like EventBridge."""

    scheduled: dict[str, tuple[datetime, dict[str, Any]]] = field(default_factory=dict)

    async def schedule(self, *, name: str, fire_at: datetime, payload: dict[str, Any]) -> None:
        self.scheduled[name] = (fire_at, payload)

    async def cancel(self, *, name: str) -> None:
        self.scheduled.pop(name, None)

    def due(self, *, now: datetime) -> list[tuple[str, dict[str, Any]]]:
        return [(name, payload) for name, (at, payload) in self.scheduled.items() if at <= now]


@dataclass
class FakeWorkQueue:
    items: list[dict[str, Any]] = field(default_factory=list)
    _dedup: set[str] = field(default_factory=set)

    async def enqueue(self, *, payload: dict[str, Any], group: str, dedup: str) -> None:
        if dedup in self._dedup:
            return
        self._dedup.add(dedup)
        self.items.append({"group": group, "dedup": dedup, **payload})

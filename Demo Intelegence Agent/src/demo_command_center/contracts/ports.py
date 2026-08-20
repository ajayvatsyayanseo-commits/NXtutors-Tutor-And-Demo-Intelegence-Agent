"""Every outbound dependency, as a Protocol.

The rule this file enforces: **no domain or capability module imports `httpx`,
`boto3`, an SDK or a URL.** They depend on the protocols here, and
`bootstrap.py` is the only module that knows which concrete class satisfies
each one. That is what lets the entire test suite, the E2E harness and
`make demo` run with no network, no AWS and no credentials — and what makes
`tests/security/test_network_boundary.py` a structural check rather than a
convention.

Protocols rather than ABCs so a fake in a test file satisfies the type without
inheriting anything, and so `mypy --strict` still checks the call sites.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from demo_command_center.contracts.tutor_match import TutorMatchRequestV1, TutorMatchResultV1
from demo_command_center.domain.messages import OutboundMessage, SendResult
from demo_command_center.domain.payments import PaymentOrder
from demo_command_center.domain.pricing import ApprovedOffer, PlanQuote
from demo_command_center.domain.slots import TimeSlot


class ProviderError(Exception):
    """Base for every adapter failure. Classified, never bare.

    `retryable` is the field the resilience layer branches on. Getting it wrong
    in the safe direction (treating a write as non-retryable) loses work;
    getting it wrong the other way double-charges someone, so adapters default
    to False and opt in.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        code: str = "",
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code
        self.code = code


class ProviderTimeout(ProviderError):
    """Timed out. Retryable for reads; for writes only with an idempotency key."""

    def __init__(self, provider: str, seconds: float) -> None:
        super().__init__(provider, f"timed out after {seconds}s", retryable=True)


class ProviderUnavailable(ProviderError):
    """5xx or a tripped circuit breaker."""

    def __init__(self, provider: str, message: str = "unavailable") -> None:
        super().__init__(provider, message, retryable=True)


class ProviderRejected(ProviderError):
    """4xx. Never retried — the same request will be rejected again."""

    def __init__(
        self, provider: str, message: str, *, status_code: int = 400, code: str = ""
    ) -> None:
        super().__init__(provider, message, retryable=False, status_code=status_code, code=code)


# ------------------------------------------------------- tutor intelligence


@runtime_checkable
class TutorIntelligencePort(Protocol):
    """Ranked tutor candidates. The implementation must not send anything."""

    async def match_tutors(self, request: TutorMatchRequestV1) -> TutorMatchResultV1: ...


# ----------------------------------------------------------- website gateway


@runtime_checkable
class NxtutorsGatewayPort(Protocol):
    """The only route to NXTutors data. No direct MySQL, ever.

    Every method takes and returns opaque refs plus already-typed models. The
    gateway is where a ref becomes a real email address, and that resolution
    happens at send time inside the adapter — it never crosses back into the
    domain.
    """

    async def resolve_identity(
        self, *, phone_hash: str, conversation_ref: str
    ) -> dict[str, Any]: ...

    async def resolve_tutor_contacts(self, *, tutor_ref: str) -> dict[str, Any]: ...

    async def tutor_availability(
        self, *, tutor_ref: str, from_at: datetime, to_at: datetime
    ) -> list[TimeSlot]: ...

    async def plan_quote(self, *, student_ref: str | None, plan_ref: str | None) -> PlanQuote: ...

    async def discount_eligibility(
        self, *, student_ref: str | None, lookback_days: int
    ) -> dict[str, Any]: ...

    async def record_demo(
        self, *, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    async def activate_subscription(
        self, *, order_ref: str, student_ref: str | None, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def region_authorization(self, *, operator_ref: str) -> list[str]: ...


# ------------------------------------------------------------------- meta


@runtime_checkable
class WhatsAppPort(Protocol):
    """Delivery only. Ownership, throttling and policy are decided before this."""

    async def send(self, message: OutboundMessage, *, recipient: str) -> SendResult: ...


@runtime_checkable
class ContactResolverPort(Protocol):
    """Turns an opaque recipient ref into a deliverable address.

    Separate from `WhatsAppPort` so the sender never has to hold a directory,
    and so a ref that cannot be resolved (opted out, deleted) fails before any
    message is composed.
    """

    async def resolve(self, recipient_ref: str) -> str | None: ...

    async def opted_out(self, recipient_ref: str) -> bool: ...


# ---------------------------------------------------------------- calendar


@runtime_checkable
class CalendarPort(Protocol):
    """Google Calendar + Meet. `create_event` must be idempotent on `request_id`."""

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
    ) -> dict[str, Any]: ...

    async def patch_event(
        self, *, event_id: str, slot: TimeSlot | None = None, description: str | None = None
    ) -> dict[str, Any]: ...

    async def cancel_event(self, *, event_id: str) -> None: ...

    async def get_event(self, *, event_id: str) -> dict[str, Any]: ...


# ----------------------------------------------------------------- payments


@runtime_checkable
class PaymentPort(Protocol):
    """Cashfree. Note `create_order` takes an `ApprovedOffer`, not an amount."""

    async def create_order(
        self, *, order: PaymentOrder, offer: ApprovedOffer, return_url: str
    ) -> dict[str, Any]: ...

    async def fetch_order(self, *, order_ref: str) -> dict[str, Any]: ...


# --------------------------------------------------------------------- llm


@runtime_checkable
class LlmPort(Protocol):
    """Structured output only. There is no free-text completion method.

    Deliberate: every call site must declare a Pydantic schema, so there is no
    way to get an unvalidated blob of model output into the system. `purpose`
    routes to a configured model id — no model name appears in business logic.
    """

    async def structured(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]: ...


# ----------------------------------------------------------------- agents


@runtime_checkable
class AgentBusPort(Protocol):
    """Handoffs to other NXTutors agents. Signed, versioned, idempotent."""

    async def dispatch(self, envelope: dict[str, Any], *, url: str) -> dict[str, Any]: ...


# ------------------------------------------------------------- scheduling


@runtime_checkable
class SchedulerPort(Protocol):
    """One-shot future callbacks (EventBridge Scheduler locally faked).

    `name` is the idempotency handle: scheduling the same name twice replaces
    rather than duplicates, which is what makes reminder rescheduling safe.
    """

    async def schedule(self, *, name: str, fire_at: datetime, payload: dict[str, Any]) -> None: ...

    async def cancel(self, *, name: str) -> None: ...


@runtime_checkable
class WorkQueuePort(Protocol):
    """Deferred work. The webhook enqueues and returns; nothing runs inline."""

    async def enqueue(self, *, payload: dict[str, Any], group: str, dedup: str) -> None: ...

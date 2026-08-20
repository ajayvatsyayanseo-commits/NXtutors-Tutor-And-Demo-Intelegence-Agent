"""Repository protocols — the persistence surface the domain depends on.

Split by aggregate rather than by table. A capability that needs a demo does not
get a handle that can also write payments, which keeps the blast radius of a bug
inside one aggregate and makes the fakes small.

Two protocol methods carry the concurrency story:

* `ConversationRepository.save_transition` takes `expected_version` and raises
  `ConcurrencyConflict`. That is optimistic locking; there is no other write
  path for state.
* `SlotRepository.place_hold` raises `SlotConflict`. The exclusion is enforced
  by a unique index, not by a read-then-write, so two concurrent bookings
  cannot both succeed however the reads interleave.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from demo_command_center.contracts.ownership import Ownership
from demo_command_center.contracts.tutor_match import TutorCandidateV1
from demo_command_center.domain.demo import Demo, DemoRequest
from demo_command_center.domain.messages import OutboundMessage, SendResult
from demo_command_center.domain.objections import ObjectionAnalysisV1
from demo_command_center.domain.payments import PaymentEvent, PaymentOrder, SubscriptionActivation
from demo_command_center.domain.pricing import DiscountDecision
from demo_command_center.domain.reminders import ScheduledReminder
from demo_command_center.domain.slots import SlotHold
from demo_command_center.state.machine import StateSnapshot, TransitionResult


@runtime_checkable
class ConversationRepository(Protocol):
    async def load(self, conversation_ref: str) -> StateSnapshot: ...

    async def save_transition(
        self, result: TransitionResult, *, now: datetime, facts: dict[str, Any] | None = None
    ) -> StateSnapshot:
        """Persist the move under optimistic locking. Raises `ConcurrencyConflict`."""
        ...

    async def history(self, conversation_ref: str, *, limit: int = 50) -> list[dict[str, Any]]: ...

    async def load_ownership(self, conversation_ref: str, *, now: datetime) -> Ownership: ...

    async def save_ownership(self, ownership: Ownership) -> None: ...

    async def touch_inbound(self, conversation_ref: str, *, at: datetime) -> None:
        """Record the last inbound message time — the session-window input."""
        ...

    async def last_inbound_at(self, conversation_ref: str) -> datetime | None: ...


@runtime_checkable
class IdempotencyRepository(Protocol):
    async def claim(self, key: str, *, scope: str, now: datetime, ttl_seconds: int) -> bool:
        """True when this caller won the claim. False means already processed."""
        ...

    async def result_for(self, key: str, *, scope: str) -> dict[str, Any] | None: ...

    async def record_result(self, key: str, *, scope: str, result: dict[str, Any]) -> None: ...


@runtime_checkable
class DemoRepository(Protocol):
    async def save_request(self, request: DemoRequest) -> None: ...

    async def load_request(self, request_id: str) -> DemoRequest | None: ...

    async def request_for_conversation(self, conversation_ref: str) -> DemoRequest | None: ...

    async def save(self, demo: Demo) -> None: ...

    async def load(self, demo_id: str) -> Demo | None: ...

    async def for_conversation(self, conversation_ref: str) -> Demo | None: ...

    async def in_window(
        self, *, region: str | None, from_at: datetime, to_at: datetime
    ) -> list[Demo]: ...

    async def save_candidates(
        self,
        *,
        conversation_ref: str,
        match_session_id: str,
        candidates: tuple[TutorCandidateV1, ...],
        captured_at: datetime,
    ) -> None:
        """The snapshot a later selection is validated against."""
        ...

    async def load_candidates(self, conversation_ref: str) -> tuple[TutorCandidateV1, ...]: ...


@runtime_checkable
class SlotRepository(Protocol):
    async def place_hold(self, hold: SlotHold) -> SlotHold:
        """Atomically claim the slot. Raises `SlotConflict` if already claimed."""
        ...

    async def load_hold(self, hold_id: str) -> SlotHold | None: ...

    async def active_hold_for(self, conversation_ref: str, *, now: datetime) -> SlotHold | None: ...

    async def release(self, hold_id: str, *, now: datetime) -> None: ...

    async def confirm(self, hold_id: str, *, now: datetime) -> None: ...

    async def expire_due(self, *, now: datetime) -> list[SlotHold]: ...


@runtime_checkable
class ReminderRepository(Protocol):
    async def replace_for_demo(
        self, demo_id: str, *, revision: int, reminders: list[ScheduledReminder]
    ) -> None:
        """Cancel every pending reminder for an older revision, then insert."""
        ...

    async def due(self, *, now: datetime, limit: int = 100) -> list[ScheduledReminder]: ...

    async def mark(
        self, reminder_id: str, *, status: str, now: datetime, detail: str = ""
    ) -> None: ...

    async def cancel_for_demo(self, demo_id: str) -> int: ...

    async def sent_count(self, demo_id: str) -> int: ...


@runtime_checkable
class OutboxRepository(Protocol):
    """Transactional outbox. A row is written with the state change, published later."""

    async def enqueue(
        self, *, event: str, payload: dict[str, Any], idempotency_key: str, now: datetime
    ) -> bool: ...

    async def unpublished(self, *, limit: int = 100) -> list[dict[str, Any]]: ...

    async def mark_published(self, outbox_id: str, *, now: datetime) -> None: ...


@runtime_checkable
class MessageLogRepository(Protocol):
    """What we sent, and the delivery outcome. The duplicate-send guard.

    `claim_send` is the exclusion: it inserts the idempotency key and returns
    False if it already existed, so two workers racing the same reminder produce
    exactly one message.
    """

    async def claim_send(self, message: OutboundMessage, *, now: datetime) -> bool: ...

    async def record_result(
        self, *, idempotency_key: str, result: SendResult, now: datetime
    ) -> None: ...

    async def record_status(
        self, *, provider_message_id: str, status: str, now: datetime
    ) -> None: ...

    async def sends_since(self, *, recipient_ref: str, since: datetime) -> int: ...


@runtime_checkable
class AnalysisRepository(Protocol):
    async def save_objections(self, analysis: ObjectionAnalysisV1) -> None: ...

    async def load_objections(self, demo_id: str) -> ObjectionAnalysisV1 | None: ...

    async def save_forecast(self, record: dict[str, Any]) -> None: ...

    async def load_forecast(self, demo_id: str) -> dict[str, Any] | None: ...

    async def save_quality(self, record: dict[str, Any]) -> None: ...


@runtime_checkable
class CommerceRepository(Protocol):
    async def save_decision(self, decision: DiscountDecision) -> None: ...

    async def load_decision(self, demo_id: str) -> DiscountDecision | None: ...

    async def offers_since(self, *, student_ref: str | None, since: datetime) -> int: ...

    async def save_order(self, order: PaymentOrder) -> None: ...

    async def load_order(self, order_ref: str) -> PaymentOrder | None: ...

    async def order_for_conversation(self, conversation_ref: str) -> PaymentOrder | None: ...

    async def record_event(self, event: PaymentEvent, *, now: datetime) -> bool:
        """False when this provider event id was already recorded."""
        ...

    async def save_activation(self, activation: SubscriptionActivation) -> None: ...

    async def activation_for(self, order_ref: str) -> SubscriptionActivation | None: ...


@runtime_checkable
class OperationsRepository(Protocol):
    """Regional rollups, alert cooldowns and human cases (capability 129)."""

    async def save_rollup(self, record: dict[str, Any]) -> None: ...

    async def rollups(self, *, region: str, metric: str, limit: int) -> list[dict[str, Any]]: ...

    async def alert_fired_at(self, *, region: str, rule: str) -> datetime | None: ...

    async def record_alert(
        self, *, region: str, rule: str, now: datetime, payload: dict[str, Any]
    ) -> None: ...

    async def open_human_case(self, record: dict[str, Any]) -> str: ...

    async def audit(self, record: dict[str, Any]) -> None: ...

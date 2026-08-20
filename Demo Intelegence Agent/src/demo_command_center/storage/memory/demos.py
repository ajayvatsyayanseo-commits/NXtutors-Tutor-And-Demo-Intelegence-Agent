"""In-memory demo, slot-hold, reminder and message-log stores.

`InMemorySlotRepository.place_hold` is the important one: it models the database
unique index on `(tutor_ref, slot_minute)` with a set membership test taken
under a lock. Two coroutines racing the same slot get exactly one success and
one `SlotConflict`, which is what `tests/e2e/test_concurrency.py` asserts —
against this class, because the Data API version cannot be exercised without a
cluster.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from demo_command_center.contracts.tutor_match import TutorCandidateV1
from demo_command_center.domain.demo import Demo, DemoRequest
from demo_command_center.domain.messages import OutboundMessage, SendResult
from demo_command_center.domain.reminders import ReminderStatus, ScheduledReminder
from demo_command_center.domain.slots import HoldStatus, SlotConflict, SlotHold
from demo_command_center.shared.clock import ensure_utc


class InMemoryDemoRepository:
    def __init__(self) -> None:
        self._requests: dict[str, DemoRequest] = {}
        self._demos: dict[str, Demo] = {}
        self._candidates: dict[str, tuple[TutorCandidateV1, ...]] = {}
        self._candidate_sessions: dict[str, str] = {}

    async def save_request(self, request: DemoRequest) -> None:
        self._requests[request.request_id] = request

    async def load_request(self, request_id: str) -> DemoRequest | None:
        return self._requests.get(request_id)

    async def request_for_conversation(self, conversation_ref: str) -> DemoRequest | None:
        for request in reversed(list(self._requests.values())):
            if request.conversation_ref == conversation_ref:
                return request
        return None

    async def save(self, demo: Demo) -> None:
        self._demos[demo.demo_id] = demo

    async def load(self, demo_id: str) -> Demo | None:
        return self._demos.get(demo_id)

    async def for_conversation(self, conversation_ref: str) -> Demo | None:
        for demo in reversed(list(self._demos.values())):
            if demo.conversation_ref == conversation_ref and not demo.cancelled:
                return demo
        return None

    async def in_window(
        self, *, region: str | None, from_at: datetime, to_at: datetime
    ) -> list[Demo]:
        start, end = ensure_utc(from_at), ensure_utc(to_at)
        return [
            demo
            for demo in self._demos.values()
            if demo.slot is not None
            and start <= demo.slot.starts_at < end
            and (region is None or demo.region == region)
        ]

    async def save_candidates(
        self,
        *,
        conversation_ref: str,
        match_session_id: str,
        candidates: tuple[TutorCandidateV1, ...],
        captured_at: datetime,
    ) -> None:
        self._candidates[conversation_ref] = candidates
        self._candidate_sessions[conversation_ref] = match_session_id

    async def load_candidates(self, conversation_ref: str) -> tuple[TutorCandidateV1, ...]:
        return self._candidates.get(conversation_ref, ())


class InMemorySlotRepository:
    def __init__(self) -> None:
        self._holds: dict[str, SlotHold] = {}
        #: Models the unique index. Membership *is* the exclusion.
        self._claimed: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def place_hold(self, hold: SlotHold) -> SlotHold:
        async with self._lock:
            owner = self._claimed.get(hold.conflict_key)
            if owner is not None and owner != hold.hold_id:
                existing = self._holds.get(owner)
                # A released or expired claim is not a conflict — it is a stale
                # index entry, and refusing on it would block the slot forever.
                if existing is not None and existing.live(now=hold.created_at):
                    raise SlotConflict(hold.tutor_ref, hold.slot.starts_at, existing_hold_id=owner)
            self._claimed[hold.conflict_key] = hold.hold_id
            self._holds[hold.hold_id] = hold
            return hold

    async def load_hold(self, hold_id: str) -> SlotHold | None:
        return self._holds.get(hold_id)

    async def active_hold_for(self, conversation_ref: str, *, now: datetime) -> SlotHold | None:
        for hold in reversed(list(self._holds.values())):
            if hold.conversation_ref == conversation_ref and hold.live(now=now):
                return hold
        return None

    async def release(self, hold_id: str, *, now: datetime) -> None:
        await self._set_status(hold_id, HoldStatus.RELEASED)

    async def confirm(self, hold_id: str, *, now: datetime) -> None:
        await self._set_status(hold_id, HoldStatus.CONFIRMED)

    async def expire_due(self, *, now: datetime) -> list[SlotHold]:
        expired: list[SlotHold] = []
        for hold_id, hold in list(self._holds.items()):
            if hold.expired(now=now):
                updated = hold.model_copy(update={"status": HoldStatus.EXPIRED})
                self._holds[hold_id] = updated
                self._claimed.pop(hold.conflict_key, None)
                expired.append(updated)
        return expired

    async def _set_status(self, hold_id: str, status: HoldStatus) -> None:
        hold = self._holds.get(hold_id)
        if hold is None:
            return
        self._holds[hold_id] = hold.model_copy(update={"status": status})
        if status in (HoldStatus.RELEASED, HoldStatus.EXPIRED):
            self._claimed.pop(hold.conflict_key, None)


class InMemoryReminderRepository:
    def __init__(self) -> None:
        self._rows: dict[str, ScheduledReminder] = {}

    async def replace_for_demo(
        self, demo_id: str, *, revision: int, reminders: list[ScheduledReminder]
    ) -> None:
        for key, row in list(self._rows.items()):
            if row.demo_id == demo_id and row.status is ReminderStatus.PENDING:
                self._rows[key] = row.model_copy(update={"status": ReminderStatus.CANCELLED})
        for reminder in reminders:
            self._rows[reminder.reminder_id] = reminder

    async def due(self, *, now: datetime, limit: int = 100) -> list[ScheduledReminder]:
        return [row for row in self._rows.values() if row.due(now=now)][:limit]

    async def mark(self, reminder_id: str, *, status: str, now: datetime, detail: str = "") -> None:
        row = self._rows.get(reminder_id)
        if row is None:
            return
        self._rows[reminder_id] = row.model_copy(
            update={
                "status": ReminderStatus(status),
                "sent_at": ensure_utc(now) if status == ReminderStatus.SENT.value else row.sent_at,
                "attempts": row.attempts + 1,
                "suppression_reason": detail[:64],
            }
        )

    async def cancel_for_demo(self, demo_id: str) -> int:
        count = 0
        for key, row in list(self._rows.items()):
            if row.demo_id == demo_id and row.status is ReminderStatus.PENDING:
                self._rows[key] = row.model_copy(update={"status": ReminderStatus.CANCELLED})
                count += 1
        return count

    async def sent_count(self, demo_id: str) -> int:
        return sum(
            1
            for row in self._rows.values()
            if row.demo_id == demo_id and row.status is ReminderStatus.SENT
        )

    def all_rows(self) -> list[ScheduledReminder]:
        return list(self._rows.values())


class InMemoryMessageLog:
    def __init__(self) -> None:
        self._claims: set[str] = set()
        self._results: dict[str, dict[str, Any]] = {}
        self._sends: list[tuple[str, datetime]] = []
        self._statuses: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def claim_send(self, message: OutboundMessage, *, now: datetime) -> bool:
        async with self._lock:
            if message.idempotency_key in self._claims:
                return False
            self._claims.add(message.idempotency_key)
            self._sends.append((message.recipient_ref, ensure_utc(now)))
            return True

    async def record_result(
        self, *, idempotency_key: str, result: SendResult, now: datetime
    ) -> None:
        self._results[idempotency_key] = {
            "outcome": result.outcome.value,
            "provider_message_id": result.provider_message_id,
            "recorded_at": ensure_utc(now).isoformat(),
        }

    async def record_status(self, *, provider_message_id: str, status: str, now: datetime) -> None:
        self._statuses[provider_message_id] = status

    async def sends_since(self, *, recipient_ref: str, since: datetime) -> int:
        floor = ensure_utc(since)
        return sum(1 for ref, at in self._sends if ref == recipient_ref and at >= floor)

    def outcomes(self) -> list[str]:
        return [row["outcome"] for row in self._results.values()]

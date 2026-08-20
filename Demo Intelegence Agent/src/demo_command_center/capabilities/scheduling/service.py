"""Capability 025 — Demo Scheduling.

Negotiate a time, hold it atomically, confirm the tutor, create one calendar
event with one Meet conference, and handle reschedules and cancellations.

The ordering here is the whole capability, and each step exists because the
obvious shortcut is wrong:

1. **Intersect availability before proposing.** Offering a slot the tutor cannot
   take produces a confirmation request that is always declined.
2. **Hold before confirming.** Between "the tutor said yes" and "the event is
   created" there is a window; without a hold, a second parent takes the slot
   inside it.
3. **Re-validate immediately before booking.** A hold makes the claim exclusive
   within *our* system. The tutor's own calendar may still have changed, so the
   last check happens against the gateway, not against our hold.
4. **Patch, never re-create, on reschedule.** One logical calendar event per
   demo; anything else sends a parent a second invite and leaves the first.

This module returns results; it never sends. Messages are `OutboundMessage`
values handed back to the orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from demo_command_center.capabilities.scheduling.ranking import rank_slots
from demo_command_center.contracts.common import DemoMode
from demo_command_center.contracts.ports import (
    CalendarPort,
    NxtutorsGatewayPort,
    ProviderError,
)
from demo_command_center.domain.demo import Demo
from demo_command_center.domain.slots import (
    DEFAULT_HOLD_TTL,
    SlotConflict,
    SlotHold,
    SlotProposal,
    TimeSlot,
    new_hold,
)
from demo_command_center.observability.logging import get_logger
from demo_command_center.repositories.ports import DemoRepository, SlotRepository
from demo_command_center.security.urls import MEET_POLICY, UrlRejected, validate
from demo_command_center.shared.clock import Clock
from demo_command_center.shared.ids import conference_request_id, hold_id

logger = get_logger("capability.scheduling")

#: How far ahead we look for mutual availability. Two weeks is long enough to
#: find a slot and short enough that the availability data is still meaningful.
SEARCH_HORIZON = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class ProposalResult:
    proposals: tuple[SlotProposal, ...] = ()
    degraded: tuple[str, ...] = ()
    reason: str = ""

    @property
    def empty(self) -> bool:
        return not self.proposals


@dataclass(frozen=True, slots=True)
class BookingResult:
    """The outcome of turning a confirmed hold into a real calendar event."""

    demo: Demo | None = None
    calendar_event_id: str = ""
    meet_url: str = ""
    failed: bool = False
    failure_reason: str = ""
    #: Set when the failure requires releasing the hold. The orchestrator runs
    #: the compensation; this flag is how it knows to.
    needs_compensation: bool = False


@dataclass(slots=True)
class SchedulingCapability:
    slots: SlotRepository
    demos: DemoRepository
    calendar: CalendarPort
    gateway: NxtutorsGatewayPort
    clock: Clock
    hold_ttl: timedelta = DEFAULT_HOLD_TTL
    degraded: list[str] = field(default_factory=list)

    # ------------------------------------------------------------- proposing
    async def propose(
        self,
        *,
        tutor_ref: str,
        preferred: TimeSlot | None,
        timezone: str,
        duration_minutes: int = 45,
        limit: int = 3,
    ) -> ProposalResult:
        """Rank mutually-available slots. Never invents availability.

        A gateway failure degrades to "we could not check" rather than to "the
        tutor is free" — offering an unverified slot is how a tutor finds a
        stranger in their calendar.
        """
        now = self.clock.now()
        try:
            available = await self.gateway.tutor_availability(
                tutor_ref=tutor_ref, from_at=now, to_at=now + SEARCH_HORIZON
            )
        except ProviderError as exc:
            logger.warning(
                "tutor availability unavailable",
                extra={"dcc_provider": exc.provider, "dcc_retryable": str(exc.retryable)},
            )
            return ProposalResult(degraded=("tutor_availability",), reason="availability_unknown")

        bookable = [slot for slot in available if slot.bookable_from(now=now) is None]
        if not bookable:
            return ProposalResult(reason="no_available_slots")

        ranked = rank_slots(
            bookable,
            preferred=preferred,
            timezone=timezone,
            duration_minutes=duration_minutes,
            now=now,
            limit=limit,
        )
        return ProposalResult(proposals=ranked)

    # ---------------------------------------------------------------- holding
    async def hold(
        self, *, conversation_ref: str, tutor_ref: str, slot: TimeSlot, mode: DemoMode
    ) -> SlotHold | None:
        """Claim the slot exclusively. None means someone else won the race."""
        now = self.clock.now()
        candidate = new_hold(
            hold_id=hold_id(now=now),
            conversation_ref=conversation_ref,
            tutor_ref=tutor_ref,
            slot=slot,
            mode=mode,
            now=now,
            ttl=self.hold_ttl,
        )
        try:
            return await self.slots.place_hold(candidate)
        except SlotConflict as exc:
            logger.info(
                "slot hold conflict",
                extra={"dcc_existing_hold": exc.existing_hold_id, "dcc_reason": "slot_taken"},
            )
            return None

    async def release(self, hold_ref: str) -> None:
        await self.slots.release(hold_ref, now=self.clock.now())

    # ---------------------------------------------------------------- booking
    async def book(
        self,
        *,
        demo: Demo,
        hold: SlotHold,
        tutor_email: str | None,
        student_email: str | None,
        summary: str,
        description: str,
    ) -> BookingResult:
        """Create the calendar event and, for online demos, the Meet link.

        Re-validates availability first. That second check is not paranoia: the
        hold is ours, but the tutor's own calendar is not, and the gap between
        proposing and booking is measured in minutes of human response time.
        """
        now = self.clock.now()
        if not hold.live(now=now):
            return BookingResult(failed=True, failure_reason="hold_expired")

        still_free = await self._revalidate(hold)
        if not still_free:
            return BookingResult(
                failed=True, failure_reason="tutor_no_longer_available", needs_compensation=True
            )

        attendees = tuple(email for email in (tutor_email, student_email) if email)
        online = demo.mode is DemoMode.ONLINE
        try:
            created = await self.calendar.create_event(
                summary=summary,
                description=description,
                slot=hold.slot,
                attendee_emails=attendees,
                with_conference=online,
                conference_request_id=conference_request_id(),
                location=None if online else demo.location_label,
            )
        except ProviderError as exc:
            logger.warning("calendar create failed", extra={"dcc_provider": exc.provider})
            return BookingResult(
                failed=True,
                failure_reason=f"calendar_error:{exc.code or 'unknown'}",
                needs_compensation=True,
            )

        event_id = str(created.get("event_id") or "")
        if not event_id:
            return BookingResult(
                failed=True, failure_reason="calendar_event_id_missing", needs_compensation=True
            )

        meet_url = ""
        if online:
            meet_url = self._verified_meet_url(created)
            if not meet_url:
                # A demo we told a parent is online, with no link, is worse than
                # a failed booking they can retry. Undo and surface it.
                await self.calendar.cancel_event(event_id=event_id)
                return BookingResult(
                    failed=True, failure_reason="meet_link_missing", needs_compensation=True
                )

        await self.slots.confirm(hold.hold_id, now=now)
        booked = demo.model_copy(
            update={
                "slot": hold.slot,
                "tutor_ref": hold.tutor_ref,
                "calendar_event_id": event_id,
                "meet_url": meet_url or None,
                "updated_at": now,
            }
        )
        await self.demos.save(booked)
        return BookingResult(demo=booked, calendar_event_id=event_id, meet_url=meet_url)

    # ------------------------------------------------------------ rescheduling
    async def reschedule(self, *, demo: Demo, slot: TimeSlot) -> BookingResult:
        """Move the existing event. One logical event per demo, always."""
        now = self.clock.now()
        if not demo.calendar_event_id:
            return BookingResult(failed=True, failure_reason="no_event_to_reschedule")
        try:
            await self.calendar.patch_event(event_id=demo.calendar_event_id, slot=slot)
        except ProviderError as exc:
            return BookingResult(failed=True, failure_reason=f"calendar_patch_failed:{exc.code}")

        moved = demo.with_slot(slot, now=now)
        await self.demos.save(moved)
        return BookingResult(
            demo=moved, calendar_event_id=demo.calendar_event_id, meet_url=demo.meet_url or ""
        )

    async def cancel(self, *, demo: Demo, reason: str) -> Demo:
        now = self.clock.now()
        if demo.calendar_event_id:
            try:
                await self.calendar.cancel_event(event_id=demo.calendar_event_id)
            except ProviderError:
                # The demo is cancelled in our system regardless; a stale
                # calendar entry is a cleanup job, not a reason to keep a
                # cancelled demo alive.
                logger.warning("calendar cancel failed; demo cancelled locally")
        cancelled = demo.model_copy(
            update={"cancelled_at": now, "cancellation_reason": reason[:120], "updated_at": now}
        )
        await self.demos.save(cancelled)
        return cancelled

    # ------------------------------------------------------------- internals
    async def _revalidate(self, hold: SlotHold) -> bool:
        """Confirm the tutor is still free for exactly this slot."""
        try:
            available = await self.gateway.tutor_availability(
                tutor_ref=hold.tutor_ref,
                from_at=hold.slot.starts_at - timedelta(minutes=1),
                to_at=hold.slot.ends_at + timedelta(minutes=1),
            )
        except ProviderError:
            # Unverifiable is not the same as unavailable. We hold an exclusive
            # claim already; proceeding on it is the lesser risk, and the
            # degradation is recorded.
            self.degraded.append("availability_revalidation")
            return True
        return any(slot.overlaps(hold.slot) for slot in available)

    @staticmethod
    def _verified_meet_url(created: dict[str, Any]) -> str:
        """A Meet URL is only accepted if it is genuinely a Google one.

        The URL comes back from an external API. Validating it against
        `MEET_POLICY` is what makes "the link we sent is authentic" a checked
        property rather than a trusted one.
        """
        raw = str(created.get("meet_url") or "")
        if not raw:
            return ""
        try:
            return validate(raw, MEET_POLICY)
        except UrlRejected:
            logger.error("calendar returned a non-Google conference URL; refusing it")
            return ""

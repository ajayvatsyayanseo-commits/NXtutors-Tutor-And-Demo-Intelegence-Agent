"""Slots, holds and the timezone rules around them.

Every instant in this module is UTC. The IANA zone travels alongside as a
separate field and is applied only at render time. Storing local time plus a
zone would mean every comparison has to convert first, and the one place someone
forgets is the one that double-books a tutor across a DST boundary.

A `SlotHold` is a short-lived exclusive claim, not a booking. It exists because
"check availability, then book" is a race: two parents can both pass the check.
The hold makes the claim atomic at the database (a unique index on the tutor and
the slot), and its TTL means a crashed process cannot block a tutor's calendar
forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import DemoMode, TutorRef
from demo_command_center.shared.clock import ensure_utc

#: How long a hold survives without being confirmed. Long enough for a tutor to
#: answer a WhatsApp confirmation, short enough that an abandoned negotiation
#: does not block a popular tutor's evening.
DEFAULT_HOLD_TTL = timedelta(minutes=20)

#: Demos are not booked further out than this. A slot four months away is
#: a data-entry error or a parsing bug, not a plan.
MAX_BOOKING_HORIZON = timedelta(days=60)

#: Nor closer than this — there is no time to confirm the tutor and send a link.
MIN_BOOKING_LEAD = timedelta(minutes=30)

DEFAULT_DEMO_MINUTES = 45


class HoldStatus(StrEnum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    RELEASED = "released"
    EXPIRED = "expired"


class SlotConflict(Exception):
    """Another hold or booking already owns this tutor/time.

    Raised by the repository when the unique index rejects the insert. Carrying
    the conflicting hold id makes the "two concurrent bookings" test able to
    assert *which* one won rather than merely that one did.
    """

    def __init__(self, tutor_ref: str, starts_at: datetime, *, existing_hold_id: str = "") -> None:
        super().__init__(f"tutor {tutor_ref} is already held at {starts_at.isoformat()}")
        self.tutor_ref = tutor_ref
        self.starts_at = starts_at
        self.existing_hold_id = existing_hold_id


def resolve_timezone(name: str) -> ZoneInfo:
    """An IANA zone, or a hard failure. Never a silent fallback to UTC.

    A silent fallback books an Indian parent's "6pm" at 23:30 local. Failing
    loudly turns that into a validation error at the edge, where it is cheap.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc


class TimeSlot(BaseModel):
    """A concrete UTC interval, with the zone it should be rendered in."""

    model_config = ConfigDict(frozen=True)

    starts_at: datetime
    duration_minutes: int = Field(default=DEFAULT_DEMO_MINUTES, ge=15, le=180)
    #: IANA name. Validated on construction so an unknown zone cannot be stored.
    timezone: str = "Asia/Kolkata"

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        object.__setattr__(self, "starts_at", ensure_utc(self.starts_at))
        resolve_timezone(self.timezone)
        return self

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_minutes)

    def overlaps(self, other: TimeSlot) -> bool:
        """Half-open `[start, end)`, so back-to-back demos do not collide."""
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at

    def local_start(self) -> datetime:
        return self.starts_at.astimezone(resolve_timezone(self.timezone))

    def label(self) -> str:
        """`Tue 12 Aug, 6:00 PM IST` — what a parent reads in a free-text message."""
        local = self.local_start()
        return f"{self.label_without_zone()} {local.strftime('%Z') or self.timezone}"

    def label_without_zone(self) -> str:
        """`Tue 12 Aug, 6:00 PM` — for a template that carries the zone separately.

        The approved reminder templates render `Date and time: {{1}}` and
        `Time zone: {{2}}` as two fields, so repeating `IST` inside {{1}} would
        read as a bug to whoever receives it.

        `%-I` is not portable to Windows, so the hour is stripped by hand.
        """
        local = self.local_start()
        hour = local.strftime("%I").lstrip("0") or "12"
        return f"{local.strftime('%a %d %b')}, {hour}:{local.strftime('%M %p')}"

    def bookable_from(self, *, now: datetime) -> str | None:
        """None when the slot is inside the allowed booking window."""
        delta = self.starts_at - ensure_utc(now)
        if delta < MIN_BOOKING_LEAD:
            return "slot_too_soon"
        if delta > MAX_BOOKING_HORIZON:
            return "slot_beyond_horizon"
        return None


class SlotProposal(BaseModel):
    """A ranked slot offered to a parent, with why it was ranked there."""

    model_config = ConfigDict(frozen=True)

    slot: TimeSlot
    rank: int = Field(ge=1, le=5)
    #: 0..1. Deterministic — computed by `capabilities/scheduling/ranking.py`
    #: from availability overlap and stated preference, never by a model.
    score: float = Field(ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = ()


class SlotHold(BaseModel):
    """An exclusive, expiring claim on one tutor at one time."""

    model_config = ConfigDict(frozen=True)

    hold_id: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    tutor_ref: TutorRef
    slot: TimeSlot
    mode: DemoMode
    status: HoldStatus = HoldStatus.ACTIVE
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _utc_and_ordered(self) -> Self:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "expires_at", ensure_utc(self.expires_at))
        if self.expires_at <= self.created_at:
            raise ValueError("hold must expire after it is created")
        return self

    def live(self, *, now: datetime) -> bool:
        return self.status is HoldStatus.ACTIVE and ensure_utc(now) < self.expires_at

    def expired(self, *, now: datetime) -> bool:
        return self.status is HoldStatus.ACTIVE and ensure_utc(now) >= self.expires_at

    @property
    def conflict_key(self) -> str:
        """The value the unique index is built on.

        Minute granularity, not second: two proposals for "6pm" that differ by
        a few seconds of clock skew are the same slot to a human and must
        collide. Including the tutor makes the claim per-tutor, which is the
        only exclusivity we actually need — one tutor cannot be in two demos.
        """
        return f"{self.tutor_ref}|{self.slot.starts_at.strftime('%Y%m%dT%H%M')}"


def new_hold(
    *,
    hold_id: str,
    conversation_ref: str,
    tutor_ref: str,
    slot: TimeSlot,
    mode: DemoMode,
    now: datetime,
    ttl: timedelta = DEFAULT_HOLD_TTL,
) -> SlotHold:
    return SlotHold(
        hold_id=hold_id,
        conversation_ref=conversation_ref,
        tutor_ref=tutor_ref,
        slot=slot,
        mode=mode,
        created_at=now,
        expires_at=now + ttl,
    )

"""Scheduled reminders and their lifecycle.

A reminder is a row, not a timer. That is what makes reschedule correct: moving
a demo cancels the pending rows for the old revision and writes new ones, so a
parent who reschedules three times receives one ladder, not three. `demo_revision`
is the whole mechanism — a reminder whose revision is behind the demo's is
obsolete by definition and needs no join to detect.

Quiet hours defer rather than drop, but never past the demo. A T-24h reminder
pushed from 02:40 to 08:00 is still useful; a T-15m reminder pushed to the next
morning is noise about something that already happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import SCHEMA_VERSION, Party
from demo_command_center.domain.slots import resolve_timezone
from demo_command_center.shared.clock import ensure_utc


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    #: The demo moved or was cancelled. Distinct from SUPPRESSED so "we changed
    #: our mind" and "policy said no" stay separable in the metrics.
    CANCELLED = "cancelled"
    SUPPRESSED = "suppressed"
    FAILED = "failed"


class ScheduledReminder(BaseModel):
    """One planned reminder for one audience on one demo revision."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    reminder_id: str = Field(max_length=64)
    demo_id: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    #: Which demo revision this belongs to. Behind the demo's = obsolete.
    demo_revision: int = Field(ge=1)

    label: str = Field(max_length=32)
    audience: Party
    recipient_ref: str = Field(max_length=128)
    template: str = Field(max_length=64)
    channel: str = Field(default="whatsapp", max_length=32)

    #: When it should go out, already quiet-hours adjusted.
    fire_at: datetime
    #: The demo start it was computed from. Kept so an obsolete reminder can be
    #: explained without loading the demo.
    demo_starts_at: datetime
    status: ReminderStatus = ReminderStatus.PENDING
    attempts: int = Field(default=0, ge=0, le=5)
    sent_at: datetime | None = None
    suppression_reason: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def _utc(self) -> Self:
        object.__setattr__(self, "fire_at", ensure_utc(self.fire_at))
        object.__setattr__(self, "demo_starts_at", ensure_utc(self.demo_starts_at))
        return self

    @property
    def idempotency_key(self) -> str:
        """One send per (demo, revision, label, audience). Reschedules make a
        new revision and therefore legitimately a new key."""
        return f"rem:{self.demo_id}:{self.demo_revision}:{self.label}:{self.audience.value}"

    def obsolete_for(self, demo_revision: int) -> bool:
        return self.demo_revision < demo_revision

    def due(self, *, now: datetime) -> bool:
        return self.status is ReminderStatus.PENDING and ensure_utc(now) >= self.fire_at

    def overdue(self, *, now: datetime, tolerance: timedelta = timedelta(minutes=10)) -> bool:
        """Past its usefulness. A late reminder is dropped, not sent."""
        return ensure_utc(now) > self.fire_at + tolerance


def in_quiet_hours(moment: datetime, *, timezone: str, start_hour: int, end_hour: int) -> bool:
    """Whether a local wall-clock time falls in the quiet window.

    Handles the wrap: `start=21, end=8` means 21:00–23:59 *and* 00:00–07:59. The
    non-wrapping case (`start=1, end=6`) is the plain interval. Getting this
    backwards silently inverts the policy and suppresses every daytime send.
    """
    hour = moment.astimezone(resolve_timezone(timezone)).hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def defer_past_quiet_hours(
    fire_at: datetime,
    *,
    timezone: str,
    start_hour: int,
    end_hour: int,
    defer_to_hour: int,
    demo_starts_at: datetime,
) -> datetime | None:
    """Move a quiet-hours send to the next allowed hour, or drop it.

    Returns None when deferring would land after the demo has already started —
    at which point the reminder has no job left to do.
    """
    if not in_quiet_hours(fire_at, timezone=timezone, start_hour=start_hour, end_hour=end_hour):
        return fire_at

    zone = resolve_timezone(timezone)
    local = fire_at.astimezone(zone)
    candidate = local.replace(hour=defer_to_hour, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    deferred = candidate.astimezone(fire_at.tzinfo)
    return None if deferred >= demo_starts_at else deferred

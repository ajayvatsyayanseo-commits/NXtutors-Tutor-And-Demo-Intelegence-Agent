"""Time, injected rather than read.

Every scheduling, reminder, hold-expiry and no-show decision in this system is a
comparison against "now". If "now" comes from `datetime.now()` inside those
functions, none of them is testable without sleeping, and the slot-hold race in
`capabilities/scheduling` cannot be exercised at all. So there is exactly one
`Clock` protocol, it is passed in, and production uses `SystemClock`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current instant, always timezone-aware and always UTC."""
        ...


class SystemClock:
    """Production clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock. Advances only when told to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment.astimezone(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes at the boundary instead of guessing a zone.

    A naive datetime here is always a bug — either a parser that dropped the
    offset or a caller passing local wall-clock time. Guessing UTC would book a
    demo 5.5 hours from where the parent meant it in the common Indian case.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime crossed a boundary that requires UTC")
    return value.astimezone(UTC)

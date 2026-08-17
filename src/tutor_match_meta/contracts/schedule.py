"""Timezone-aware weekly schedule primitives.

Availability is the dimension most likely to be quietly wrong, so the types are
deliberately strict: a window is a half-open `[start, end)` interval on a named
weekday in a named timezone, and every overlap question is answered by comparing
absolute minute offsets inside a single canonical week.

Windows that wrap past midnight are split at the day boundary on construction, so
no downstream code ever has to reason about `end < start`.
"""

from __future__ import annotations

from datetime import time
from enum import IntEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY


class Weekday(IntEnum):
    """Monday = 0, matching `datetime.weekday()`."""

    MON = 0
    TUE = 1
    WED = 2
    THU = 3
    FRI = 4
    SAT = 5
    SUN = 6

    @property
    def short(self) -> str:
        return ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[self.value]


WEEKEND: frozenset[Weekday] = frozenset({Weekday.SAT, Weekday.SUN})
WEEKDAYS: frozenset[Weekday] = frozenset(set(Weekday) - WEEKEND)


class TimeWindow(BaseModel):
    """A half-open `[start, end)` window on one weekday.

    `end == 00:00` means end-of-day (24:00), the only way to express a window
    that runs to midnight without allowing `end < start` elsewhere.
    """

    model_config = ConfigDict(frozen=True)

    weekday: Weekday
    start: time
    end: time

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        if self.end_minute <= self.start_minute:
            raise ValueError(
                f"window end must be after start: {self.weekday.short} "
                f"{self.start.isoformat()}-{self.end.isoformat()}"
            )
        return self

    @property
    def start_minute(self) -> int:
        """Minutes from Monday 00:00 in the owning schedule's timezone."""
        return self.weekday * MINUTES_PER_DAY + self.start.hour * 60 + self.start.minute

    @property
    def end_minute(self) -> int:
        end = self.end.hour * 60 + self.end.minute
        if end == 0:
            end = MINUTES_PER_DAY  # 00:00 as an end bound means 24:00.
        return self.weekday * MINUTES_PER_DAY + end

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute

    def overlap_minutes(self, other: TimeWindow) -> int:
        """Minutes shared with `other`. Zero when they do not intersect."""
        return max(
            0, min(self.end_minute, other.end_minute) - max(self.start_minute, other.start_minute)
        )

    def overlaps(self, other: TimeWindow) -> bool:
        return self.overlap_minutes(other) > 0

    def contains(self, other: TimeWindow) -> bool:
        return self.start_minute <= other.start_minute and self.end_minute >= other.end_minute

    def intersect(self, other: TimeWindow) -> TimeWindow | None:
        if not self.overlaps(other):
            return None
        start = max(self.start_minute, other.start_minute)
        end = min(self.end_minute, other.end_minute)
        return _window_from_minutes(start, end)

    def label(self) -> str:
        end = self.end.strftime("%H:%M") if self.end != time(0, 0) else "24:00"
        return f"{self.weekday.short} {self.start.strftime('%H:%M')}-{end}"


def _window_from_minutes(start_minute: int, end_minute: int) -> TimeWindow:
    """Rebuild a window from absolute week-minutes. Must not cross a day."""
    weekday = Weekday(start_minute // MINUTES_PER_DAY)
    day_start = weekday * MINUTES_PER_DAY
    start_offset = start_minute - day_start
    end_offset = end_minute - day_start
    if end_offset > MINUTES_PER_DAY:
        raise ValueError("window crosses a day boundary; split it first")
    end = time(0, 0) if end_offset == MINUTES_PER_DAY else time(end_offset // 60, end_offset % 60)
    return TimeWindow(weekday=weekday, start=time(start_offset // 60, start_offset % 60), end=end)


class WeeklySchedule(BaseModel):
    """A set of windows in one timezone, normalised on construction.

    Normalisation merges touching/overlapping windows on the same weekday, so
    `overlap_minutes` never double-counts and a schedule has exactly one
    canonical form regardless of how it was assembled.
    """

    model_config = ConfigDict(frozen=True)

    timezone: str = "Asia/Kolkata"
    windows: tuple[TimeWindow, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _normalise(self) -> Self:
        merged = _merge(self.windows)
        if merged != self.windows:
            object.__setattr__(self, "windows", merged)
        return self

    def __bool__(self) -> bool:
        return bool(self.windows)

    @property
    def total_minutes(self) -> int:
        return sum(w.duration_minutes for w in self.windows)

    def overlap_minutes(self, other: WeeklySchedule) -> int:
        """Shared minutes with `other`.

        Both schedules must already be expressed in the same timezone; the
        caller converts (see `domain.scheduling.to_timezone`) rather than having
        this silently guess, because a silent tz coercion is exactly the bug
        that produces "available at 6:30" for the wrong 6:30.
        """
        if self.timezone != other.timezone:
            raise ValueError(
                f"cannot overlap schedules across timezones: {self.timezone} vs {other.timezone}"
            )
        return sum(a.overlap_minutes(b) for a in self.windows for b in other.windows)

    def intersection(self, other: WeeklySchedule) -> WeeklySchedule:
        if self.timezone != other.timezone:
            raise ValueError("cannot intersect schedules across timezones")
        shared = [x for a in self.windows for b in other.windows if (x := a.intersect(b))]
        return WeeklySchedule(timezone=self.timezone, windows=tuple(shared))

    def weekdays(self) -> frozenset[Weekday]:
        return frozenset(w.weekday for w in self.windows)

    def labels(self, limit: int = 4) -> list[str]:
        return [w.label() for w in self.windows[:limit]]


def _merge(windows: tuple[TimeWindow, ...]) -> tuple[TimeWindow, ...]:
    if not windows:
        return ()
    ordered = sorted(windows, key=lambda w: (w.start_minute, w.end_minute))
    out: list[TimeWindow] = [ordered[0]]
    for window in ordered[1:]:
        last = out[-1]
        # Merge only within the same weekday; touching (>=) counts as adjacent so
        # 16:00-18:00 + 18:00-20:00 becomes one window rather than two.
        if window.weekday == last.weekday and window.start_minute <= last.end_minute:
            if window.end_minute > last.end_minute:
                out[-1] = _window_from_minutes(last.start_minute, window.end_minute)
        else:
            out.append(window)
    return tuple(out)

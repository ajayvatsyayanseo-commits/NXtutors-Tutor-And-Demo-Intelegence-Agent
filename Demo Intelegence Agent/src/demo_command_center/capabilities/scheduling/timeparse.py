"""Deterministic natural-language time interpretation.

Runs **before** the LLM and, for everything it recognises, instead of it. "kal
shaam 6 baje" and "tomorrow 6pm" are the overwhelming majority of what parents
actually type, and a regex answers both in microseconds with a result that is
identical on every run. The model is the fallback for the long tail, and even
then it returns a *candidate* that is re-validated here — `interpret()` is the
only function that produces a `TimeSlot`.

Every resolution is relative to a caller-supplied `now` in a caller-supplied
zone. There is no `datetime.now()` in this module, which is what makes "next
Tuesday" testable and what stops a Lambda in `ap-south-1` and one in `us-east-1`
disagreeing about what day it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from demo_command_center.domain.slots import TimeSlot, resolve_timezone

#: Hindi/Hinglish and English day words, mapped to a day offset from today.
_RELATIVE_DAYS: dict[str, int] = {
    "today": 0, "aaj": 0, "aj": 0,
    "tomorrow": 1, "kal": 1, "tmrw": 1, "tmr": 1,
    "day after tomorrow": 2, "parso": 2, "parsu": 2,
}  # fmt: skip

_WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0, "somvar": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "mangalvar": 1,
    "wednesday": 2, "wed": 2, "budhvar": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "guruvar": 3,
    "friday": 4, "fri": 4, "shukravar": 4,
    "saturday": 5, "sat": 5, "shanivar": 5,
    "sunday": 6, "sun": 6, "ravivar": 6,
}  # fmt: skip

#: Part-of-day words → the hour we assume when no number is given. Conservative
#: on purpose: "evening" is 18:00, not 17:00, because a tuition demo booked an
#: hour before the parent expected is worse than one they must adjust.
_DAYPARTS: dict[str, int] = {
    "morning": 9, "subah": 9, "savere": 9,
    "afternoon": 14, "dopahar": 14,
    "evening": 18, "shaam": 18, "sham": 18,
    "night": 20, "raat": 20,
}  # fmt: skip

_TIME_RE = re.compile(
    r"\b(?P<hour>[01]?\d|2[0-3])"
    r"(?:[:.](?P<minute>[0-5]\d))?"
    r"\s*(?P<meridiem>a\.?m\.?|p\.?m\.?|baje)?\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"\b(?P<day>[0-3]?\d)[/\-.](?P<month>[01]?\d)(?:[/\-.](?P<year>\d{2,4}))?\b")

#: Hours a demo may start. Outside this, a parsed time is almost always a
#: misparse ("class 10" is not 10 o'clock) and is rejected rather than booked.
EARLIEST_HOUR = 6
LATEST_HOUR = 22


@dataclass(frozen=True, slots=True)
class TimeGuess:
    """A parsed time, with how it was reached. `confident` gates auto-booking."""

    slot: TimeSlot | None
    confident: bool
    reason: str
    #: Set when the text names a day but no time, so the assistant can ask for
    #: just the missing half rather than the whole thing again.
    partial_day: datetime | None = None


def interpret(
    text: str,
    *,
    now: datetime,
    timezone: str,
    duration_minutes: int = 45,
) -> TimeGuess:
    """Resolve free text to a slot. The only producer of a `TimeSlot` from text."""
    zone = resolve_timezone(timezone)
    local_now = now.astimezone(zone)
    lowered = " ".join(text.lower().split())

    day = _resolve_day(lowered, local_now)
    hour_minute, hour_confident, marked = _resolve_time(lowered)

    if day is None and hour_minute is None:
        return TimeGuess(None, False, "no_temporal_expression")

    if day is None and not marked:
        # A bare number with no day and no time marker is not a time. "my son is
        # in class 10" and "we need 2 sessions a week" both contain a plausible
        # hour, and booking either as a demo slot is the misparse that produces
        # a calendar invite nobody asked for. A marker is an am/pm, a "baje", or
        # a part-of-day word.
        return TimeGuess(None, False, "no_temporal_expression")

    if hour_minute is None:
        # A day with no time. Genuinely useful: ask only for the hour.
        return TimeGuess(None, False, "time_missing", partial_day=day)

    hour, minute = hour_minute
    if not EARLIEST_HOUR <= hour <= LATEST_HOUR:
        return TimeGuess(None, False, f"hour_out_of_range:{hour}")

    base = day or local_now
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day is None and candidate <= local_now:
        # "6pm" said at 7pm means tomorrow. Rolling forward is right; booking
        # something in the past is never right.
        candidate = candidate + timedelta(days=1)

    slot = TimeSlot(
        starts_at=candidate.astimezone(now.tzinfo),
        duration_minutes=duration_minutes,
        timezone=timezone,
    )
    if slot.bookable_from(now=now) is not None:
        return TimeGuess(None, False, slot.bookable_from(now=now) or "unbookable")

    return TimeGuess(slot, hour_confident and day is not None, "parsed")


def _resolve_day(text: str, local_now: datetime) -> datetime | None:
    """Relative word, weekday name or an explicit date. First match wins."""
    for phrase, offset in sorted(_RELATIVE_DAYS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            return local_now + timedelta(days=offset)

    for name, weekday in _WEEKDAYS.items():
        if not re.search(rf"\b{re.escape(name)}\b", text):
            continue
        ahead = (weekday - local_now.weekday()) % 7
        # "on Tuesday" said on a Tuesday means next Tuesday, not right now.
        if ahead == 0:
            ahead = 7
        if "next" in text and ahead < 7:
            ahead += 7
        return local_now + timedelta(days=ahead)

    match = _DATE_RE.search(text)
    if match:
        day = int(match.group("day"))
        month = int(match.group("month"))
        raw_year = match.group("year")
        year = local_now.year if raw_year is None else int(raw_year)
        if year < 100:
            year += 2000
        try:
            candidate = local_now.replace(
                year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0
            )
        except ValueError:
            return None
        # A bare `12/08` that has already passed means next year.
        if raw_year is None and candidate.date() < local_now.date():
            candidate = candidate.replace(year=year + 1)
        return candidate
    return None


def _resolve_time(text: str) -> tuple[tuple[int, int] | None, bool, bool]:
    """`((hour, minute), confident, marked)`.

    `marked` says the text carried an actual time marker — an am/pm, a "baje",
    or a part-of-day word — rather than just a number that happens to look like
    an hour. `confident` is stricter still and requires an explicit meridiem.
    """
    for match in _TIME_RE.finditer(text):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        meridiem = (match.group("meridiem") or "").replace(".", "").lower()

        if meridiem.startswith("p") and hour < 12:
            hour += 12
        elif meridiem.startswith("a") and hour == 12:
            hour = 0
        elif not meridiem or meridiem == "baje":
            # "6 baje" / bare "6" for a tuition demo means 18:00, not 06:00.
            # Only promote hours that are implausible as a morning demo time.
            if 1 <= hour <= 8:
                hour += 12
        if EARLIEST_HOUR <= hour <= LATEST_HOUR:
            return (hour, minute), meridiem.startswith(("a", "p")), bool(meridiem)

    for word, hour in _DAYPARTS.items():
        if re.search(rf"\b{re.escape(word)}\b", text):
            return (hour, 0), False, True
    return None, False, False


def describe_window(slot: TimeSlot) -> str:
    """What we echo back for confirmation. Always includes the zone.

    Echoing the zone is not decoration: it is the only way a parent can catch a
    misparse before it becomes a calendar invite at the wrong hour.
    """
    return slot.label()

"""Natural-language schedule parsing, timezone conversion and slot suggestion.

"after 6:30", "mon wed fri evenings", "weekends only", "shaam ko" all become a
`WeeklySchedule` of explicit tz-aware windows. Anything that cannot be parsed
confidently returns nothing — an invented window would make the matcher claim an
availability overlap that does not exist.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from tutor_match_meta.contracts.schedule import (
    MINUTES_PER_DAY,
    WEEKDAYS,
    WEEKEND,
    TimeWindow,
    Weekday,
    WeeklySchedule,
)
from tutor_match_meta.domain.text import ascii_fold, transliterate_hinglish

DEFAULT_TIMEZONE = "Asia/Kolkata"

_DAY_WORDS: dict[str, Weekday] = {
    "monday": Weekday.MON,
    "mon": Weekday.MON,
    "somwar": Weekday.MON,
    "tuesday": Weekday.TUE,
    "tue": Weekday.TUE,
    "tues": Weekday.TUE,
    "mangalwar": Weekday.TUE,
    "wednesday": Weekday.WED,
    "wed": Weekday.WED,
    "budhwar": Weekday.WED,
    "thursday": Weekday.THU,
    "thu": Weekday.THU,
    "thurs": Weekday.THU,
    "guruwar": Weekday.THU,
    "friday": Weekday.FRI,
    "fri": Weekday.FRI,
    "shukrawar": Weekday.FRI,
    "saturday": Weekday.SAT,
    "sat": Weekday.SAT,
    "shanivar": Weekday.SAT,
    "sunday": Weekday.SUN,
    "sun": Weekday.SUN,
    "raviwar": Weekday.SUN,
}

#: Named parts of the day. Chosen to match how Indian parents actually schedule
#: tuition: "evening" means after school and before dinner, not 17:00-23:59.
_DAYPARTS: dict[str, tuple[time, time]] = {
    "morning": (time(6, 0), time(11, 0)),
    "subah": (time(6, 0), time(11, 0)),
    "afternoon": (time(12, 0), time(16, 0)),
    "dopahar": (time(12, 0), time(16, 0)),
    "evening": (time(16, 0), time(20, 30)),
    "shaam": (time(16, 0), time(20, 30)),
    "night": (time(20, 0), time(22, 0)),
    "raat": (time(20, 0), time(22, 0)),
    "after school": (time(15, 30), time(20, 30)),
}

#: The latest a tuition window is assumed to run when the parent only gave a
#: lower bound ("after 6:30"). Policy-ish, but a hard clamp: without it, "after
#: 6:30" would claim availability at 3am.
_OPEN_END = time(22, 0)
#: Likewise the earliest, for "before 8".
_OPEN_START = time(6, 0)

_TIME = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b",
    re.IGNORECASE,
)

#: A number right after one of these is a grade, an address or a fee — not a
#: clock reading. Without this, "class 10 cbse" schedules tuition at 10:00.
_NON_TIME_PREFIX = re.compile(
    r"\b(?:class|std|standard|grade|sector|sec|phase|pin|pincode|block|plot|flat|"
    r"house|road|floor|no|number|age|marks?|budget|rs|inr|fees?|rate)\s*[-.:#]?\s*$",
    re.IGNORECASE,
)
_CURRENCY_BEFORE = re.compile(r"₹\s*$")
#: Word-boundary matched: the naive substring test finds "am" inside "exam".
_MORNING_MARKER = re.compile(r"\b(?:morning|subah|a\.?m\.?|breakfast)\b", re.IGNORECASE)
_AFTER = re.compile(r"\b(?:after|post|baad|from|onwards?|se)\b", re.IGNORECASE)
_BEFORE = re.compile(r"\b(?:before|till|until|upto|up\s*to|by|pehle|tak)\b", re.IGNORECASE)
_BETWEEN = re.compile(r"\b(?:between|from)\b.*?\b(?:and|to|[-–—])\b", re.IGNORECASE)
_WEEKEND_ONLY = re.compile(
    r"\bweekends?\b|\bsat(?:urday)?\s*(?:and|&|/)\s*sun(?:day)?\b", re.IGNORECASE
)
_WEEKDAY_ONLY = re.compile(
    r"\bweek\s*days?\b|\bmon(?:day)?\s*(?:to|[-–—])\s*fri(?:day)?\b", re.IGNORECASE
)
_DAILY = re.compile(r"\b(?:daily|everyday|every\s*day|all\s*days|roz)\b", re.IGNORECASE)
_ALTERNATE = re.compile(r"\balternate\s*days?\b|\bevery\s*other\s*day\b", re.IGNORECASE)


def _to_time(hour: int, minute: int, meridiem: str | None, *, context: str) -> time | None:
    """Resolve a clock reading, inferring AM/PM when the parent omitted it.

    Indian tuition is overwhelmingly late-afternoon/evening, so a bare "6:30" in
    a tuition context means 18:30. Bare hours 1–7 are read as PM; 8–11 stay AM
    only when the surrounding text mentions a morning word.
    """
    if minute > 59:
        return None
    marker = (meridiem or "").lower().replace(".", "")
    if marker.startswith("p"):
        hour = hour % 12 + 12
    elif marker.startswith("a"):
        hour = hour % 12
    else:
        if hour > 23:
            return None
        # 1-7 with no marker is evening in a tuition context; 8-11 is genuinely
        # ambiguous and stays AM.
        if 1 <= hour <= 7 and not _MORNING_MARKER.search(context):
            hour += 12
    return time(hour, minute) if 0 <= hour <= 23 else None


def _clock_readings(text: str) -> list[tuple[time, int]]:
    """Clock readings as `(time, position)`, position being the match offset.

    Positions matter: "after 6:30" has to bind to the reading that *follows* the
    keyword, not to whichever number happened to appear first in the sentence.
    """
    out: list[tuple[time, int]] = []
    for match in _TIME.finditer(text):
        hour_s, minute_s, meridiem = match.group(1), match.group(2), match.group(3)
        prefix = text[max(0, match.start() - 16) : match.start()]
        if _NON_TIME_PREFIX.search(prefix) or _CURRENCY_BEFORE.search(prefix):
            continue
        hour = int(hour_s)
        # A bare number with no separator and no meridiem is more likely a fee
        # or an id fragment than a time.
        if not minute_s and not meridiem and hour > 12:
            continue
        resolved = _to_time(hour, int(minute_s or 0), meridiem, context=text)
        if resolved is not None:
            out.append((resolved, match.start()))
    return out


def _first_after(readings: list[tuple[time, int]], position: int) -> time | None:
    """The first reading positioned after `position`, else the first overall."""
    for value, offset in readings:
        if offset > position:
            return value
    return readings[0][0] if readings else None


def extract_days(text: str) -> frozenset[Weekday]:
    """Weekdays named or implied by the message. Empty means unstated."""
    folded = ascii_fold(transliterate_hinglish(text)).lower()
    if _WEEKEND_ONLY.search(folded):
        return frozenset(WEEKEND)
    if _WEEKDAY_ONLY.search(folded):
        return frozenset(WEEKDAYS)
    if _DAILY.search(folded) or _ALTERNATE.search(folded):
        return frozenset(Weekday)
    named = {
        day for word, day in _DAY_WORDS.items() if re.search(rf"\b{re.escape(word)}\b", folded)
    }
    return frozenset(named)


def extract_time_range(text: str) -> tuple[time, time] | None:
    """The time band the parent described, or None when nothing is stated."""
    folded = ascii_fold(transliterate_hinglish(text)).lower()
    readings = _clock_readings(folded)
    after, before = _AFTER.search(folded), _BEFORE.search(folded)

    for name, (start, end) in _DAYPARTS.items():
        if name not in folded:
            continue
        # A daypart plus an explicit bound narrows the daypart rather than
        # replacing it: "evening after 7" is 19:00-20:30, not 19:00-22:00.
        if len(readings) >= 2:
            low, high = sorted(value for value, _ in readings[:2])
            if low < high:
                if low < end and high > start:
                    return (max(low, start), min(high, end))
                return (low, high)
        if readings and after:
            bound = _first_after(readings, after.end())
            lower = max(bound, start) if bound else start
            return (lower, end) if lower < end else (lower, _OPEN_END)
        if readings and before:
            bound = _first_after(readings, before.end())
            upper = min(bound, end) if bound else end
            return (start, upper) if start < upper else (start, end)
        return (start, end)

    if not readings:
        return None
    if _BETWEEN.search(folded) and len(readings) >= 2:
        low, high = sorted(value for value, _ in readings[:2])
        return (low, high) if low < high else None
    if after:
        bound = _first_after(readings, after.end())
        return (bound, _OPEN_END) if bound and bound < _OPEN_END else None
    if before:
        bound = _first_after(readings, before.end())
        return (_OPEN_START, bound) if bound and _OPEN_START < bound else None
    if len(readings) >= 2:
        low, high = sorted(value for value, _ in readings[:2])
        if low < high:
            return (low, high)
    # A single bare time is a start hint; assume a standard session length so we
    # never claim more availability than the parent implied.
    start = readings[0][0]
    end_minutes = min(start.hour * 60 + start.minute + 90, MINUTES_PER_DAY - 1)
    return (start, time(end_minutes // 60, end_minutes % 60))


def parse_schedule(text: str, *, timezone: str = DEFAULT_TIMEZONE) -> WeeklySchedule | None:
    """Full parse: days × time band. Returns None when neither is stated.

    When days are given but no time, the whole tuition-plausible day is used.
    When a time is given but no days, it applies to every day — the parent
    constrained the hour, not the weekday.
    """
    days = extract_days(text)
    band = extract_time_range(text)
    if not days and band is None:
        return None
    if not days:
        days = frozenset(Weekday)
    if band is None:
        band = (time(6, 0), _OPEN_END)
    start, end = band
    if start >= end:
        return None
    windows = tuple(TimeWindow(weekday=day, start=start, end=end) for day in sorted(days, key=int))
    return WeeklySchedule(timezone=timezone, windows=windows)


def to_timezone(schedule: WeeklySchedule, target: str) -> WeeklySchedule:
    """Re-express a weekly schedule in another timezone.

    Anchored on a fixed reference week so the conversion is deterministic and
    testable. Windows that cross midnight after the shift are split at the day
    boundary, preserving the invariant that a `TimeWindow` never wraps.
    """
    if schedule.timezone == target:
        return schedule
    source_tz, target_tz = ZoneInfo(schedule.timezone), ZoneInfo(target)
    # A Monday far from any DST edge in either zone. IST has no DST at all
    # (assumptions A19); this keeps the conversion stable for zones that do.
    anchor = datetime(2025, 6, 2, tzinfo=source_tz)
    shifted: list[TimeWindow] = []
    for window in schedule.windows:
        start_dt = anchor + timedelta(minutes=window.start_minute)
        end_dt = anchor + timedelta(minutes=window.end_minute)
        shifted.extend(
            _split_across_days(start_dt.astimezone(target_tz), end_dt.astimezone(target_tz))
        )
    return WeeklySchedule(timezone=target, windows=tuple(shifted))


def _split_across_days(start: datetime, end: datetime) -> list[TimeWindow]:
    out: list[TimeWindow] = []
    cursor = start
    while cursor < end:
        day_end = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        segment_end = min(end, day_end)
        end_time = (
            time(0, 0) if segment_end == day_end else time(segment_end.hour, segment_end.minute)
        )
        out.append(
            TimeWindow(
                weekday=Weekday(cursor.weekday()),
                start=time(cursor.hour, cursor.minute),
                end=end_time,
            )
        )
        cursor = segment_end
    return out


def suggest_slots(
    overlap: WeeklySchedule, *, session_minutes: int = 60, limit: int = 3
) -> list[TimeWindow]:
    """Concrete session slots inside a mutually-free schedule.

    Takes the earliest usable slot per weekday rather than several from one day,
    so a suggestion list reads like a week's plan instead of three variations of
    the same Tuesday.
    """
    by_day: dict[Weekday, TimeWindow] = {}
    for window in sorted(overlap.windows, key=lambda w: w.start_minute):
        if window.duration_minutes < session_minutes:
            continue
        if window.weekday in by_day:
            continue
        end_minute = window.start_minute + session_minutes
        day_start = window.weekday * MINUTES_PER_DAY
        by_day[window.weekday] = TimeWindow(
            weekday=window.weekday,
            start=window.start,
            end=time(
                (end_minute - day_start) // 60 % 24,
                (end_minute - day_start) % 60,
            ),
        )
    return [by_day[day] for day in sorted(by_day, key=int)][:limit]


def detect_conflicts(schedule: WeeklySchedule, commitments: WeeklySchedule) -> list[TimeWindow]:
    """Windows where a proposed schedule collides with existing commitments."""
    return [w for w in schedule.intersection(commitments).windows]

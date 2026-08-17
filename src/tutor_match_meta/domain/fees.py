"""Fee parsing and presentation.

The single most important rule here: **the website schema stores no fee unit**
(`register.budget` is free text). So a website-sourced fee is rendered as a bare
band — `₹800–₹1,200` — and never as "per hour". Quoting a per-hour rate we cannot
prove would be a fabricated commercial claim to a parent.

A fee stated by the *parent* is different: they usually say the unit out loud
("900 per hour"), so that unit is captured and preserved.
"""

from __future__ import annotations

import re
from enum import StrEnum

from tutor_match_meta.contracts.tutor import FeeBand

#: 900, 1,200, 1200/-, ₹1200, Rs 1200, 1.2k, 15k
#: The multiplier needs a trailing word boundary or "9 ka" (Hindi "of 9") parses
#: as 9,000.
_AMOUNT = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)(?:\s*(k|thousand|hazar|hazaar)\b)?",
    re.IGNORECASE,
)

#: A number sitting right after one of these is an address, a grade or an id —
#: not money. Without this, "sector 57" becomes a ₹57 budget.
_NON_FEE_PREFIX = re.compile(
    r"\b(?:sector|sec|class|std|standard|grade|phase|pin|pincode|block|plot|flat|"
    r"house|road|floor|no|number|age|marks?|std)\s*[-.:#]?\s*$",
    re.IGNORECASE,
)

#: A number near one of these really is money.
_FEE_CONTEXT = re.compile(
    r"₹|\brs\.?|\binr\b|\bbudget\b|\bfees?\b|\bcharges?\b|\brate\b|\bprice\b|"
    r"\bpay(?:ing|ment)?\b|\bafford\b|\bper\s*hour\b|\bper\s*month\b|\bhourly\b|"
    r"\bmonthly\b|\bpaisa\b|\brupees?\b",
    re.IGNORECASE,
)
_PER_HOUR = re.compile(
    r"\b(?:per\s*hour|/\s*hr|per\s*hr|an?\s*hour|hourly|ghante)\b", re.IGNORECASE
)
_PER_MONTH = re.compile(r"\b(?:per\s*month|/\s*month|monthly|mahine|p\.?m\.?)\b", re.IGNORECASE)
_PER_SESSION = re.compile(r"\b(?:per\s*(?:class|session|sitting))\b", re.IGNORECASE)
_RANGE_HINT = re.compile(r"\b(?:between|from|to|se|tak)\b|[-–—]", re.IGNORECASE)
_APPROX = re.compile(
    r"\b(?:around|approx|about|roughly|near(?:ly)?|lagbhag|takriban)\b", re.IGNORECASE
)
_MAX_HINT = re.compile(
    r"\b(?:under|below|max(?:imum)?|upto|up\s*to|within|budget\s*is)\b", re.IGNORECASE
)
_MIN_HINT = re.compile(r"\b(?:above|over|min(?:imum)?|at\s*least|more\s*than)\b", re.IGNORECASE)

#: Anything outside this is not a tutoring fee — it is a phone number, a pincode,
#: or a year that happened to sit next to a rupee sign.
PLAUSIBLE_FEE_MIN = 50
PLAUSIBLE_FEE_MAX = 200_000

#: Interpreting "around 900" as a hard ceiling loses good tutors at 950.
#: Interpreting it as unbounded ignores the parent. This is the tolerance band.
APPROX_TOLERANCE = 0.15


class FeeUnit(StrEnum):
    HOUR = "hour"
    MONTH = "month"
    SESSION = "session"
    UNKNOWN = "unknown"


class ParsedBudget:
    """A parent's stated budget, with the unit they actually used."""

    __slots__ = ("maximum", "minimum", "raw", "unit")

    def __init__(
        self,
        minimum: int | None,
        maximum: int | None,
        unit: FeeUnit,
        raw: str,
    ) -> None:
        self.minimum = minimum
        self.maximum = maximum
        self.unit = unit
        self.raw = raw

    def __bool__(self) -> bool:
        return self.minimum is not None or self.maximum is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ParsedBudget({self.minimum}, {self.maximum}, {self.unit}, {self.raw!r})"


def _amounts(text: str) -> list[int]:
    """Plausible rupee amounts in the text, in order of appearance.

    A tutoring message is full of numbers that are not money — a grade, a
    sector, a clock time, a pincode. Two filters separate them:

    1. numbers introduced by an address/grade word are dropped outright;
    2. when any number is *explicitly* monetary (currency symbol, a `k` suffix,
       or a fee word nearby), only those are returned — so "class 10 ... around
       900 per hour" yields ``[900]`` rather than ``[10, 900]``.
    """
    strong: list[int] = []
    weak: list[int] = []
    for match in _AMOUNT.finditer(text):
        raw, multiplier = match.group(1), match.group(2)
        if _NON_FEE_PREFIX.search(text[max(0, match.start() - 16) : match.start()]):
            continue
        number = float(raw.replace(",", ""))
        if multiplier:
            number *= 1000
        value = int(round(number))
        if not PLAUSIBLE_FEE_MIN <= value <= PLAUSIBLE_FEE_MAX:
            continue
        window = text[max(0, match.start() - 28) : match.end() + 28]
        if multiplier or _FEE_CONTEXT.search(window):
            strong.append(value)
        else:
            weak.append(value)
    return strong or weak


def detect_unit(text: str) -> FeeUnit:
    if _PER_HOUR.search(text):
        return FeeUnit.HOUR
    if _PER_MONTH.search(text):
        return FeeUnit.MONTH
    if _PER_SESSION.search(text):
        return FeeUnit.SESSION
    return FeeUnit.UNKNOWN


def parse_budget(text: str) -> ParsedBudget | None:
    """Extract a parent's budget from natural language.

    Handles the four shapes that actually appear in NXTutors WhatsApp traffic:
    a range ("800 to 1200"), a ceiling ("under 1000"), a floor ("at least 500"),
    and an approximation ("around 900" → a ±15% band, not a hard ceiling).
    """
    values = _amounts(text)
    if not values:
        return None
    unit = detect_unit(text)
    raw = text.strip()[:120]

    if len(values) >= 2 and _RANGE_HINT.search(text):
        return ParsedBudget(min(values[:2]), max(values[:2]), unit, raw)
    amount = values[0]
    if _MAX_HINT.search(text):
        return ParsedBudget(None, amount, unit, raw)
    if _MIN_HINT.search(text):
        return ParsedBudget(amount, None, unit, raw)
    if _APPROX.search(text):
        margin = int(round(amount * APPROX_TOLERANCE))
        return ParsedBudget(amount - margin, amount + margin, unit, raw)
    # A bare figure is read as a target with the same tolerance: parents saying
    # "900" essentially never mean "reject 920".
    margin = int(round(amount * APPROX_TOLERANCE))
    return ParsedBudget(amount - margin, amount + margin, unit, raw)


def parse_tutor_fee(budget_column: str | None) -> FeeBand:
    """Parse `register.budget`. Unit stays unknown — the column never states one.

    Mirrors Laravel's `PublicTutorFieldMapper::parseFee()`, including its
    deliberate refusal to render "per hour".
    """
    if not budget_column or not budget_column.strip():
        return FeeBand()
    values = _amounts(budget_column)
    if not values:
        return FeeBand()
    low, high = min(values), max(values)
    return FeeBand(minimum=low, maximum=high, label=format_fee_label(low, high), unit_known=False)


def format_fee_label(minimum: int | None, maximum: int | None) -> str | None:
    """`₹900` or `₹800–₹1,200`. Never carries a unit suffix (assumptions A3)."""
    if minimum is None and maximum is None:
        return None
    if minimum is not None and maximum is not None and minimum != maximum:
        return f"₹{minimum:,}–₹{maximum:,}"
    value = minimum if minimum is not None else maximum
    return f"₹{value:,}"


def parse_experience_years(raw: str | None) -> int | None:
    """`'5 years'`, `'5+ yrs'`, `'05'` -> 5. Implausible values are discarded."""
    if not raw:
        return None
    match = re.search(r"\d{1,2}", raw)
    if not match:
        return None
    years = int(match.group())
    return years if 0 <= years <= 70 else None

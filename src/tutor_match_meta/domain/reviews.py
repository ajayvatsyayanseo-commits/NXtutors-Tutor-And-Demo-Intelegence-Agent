"""Review aggregation and low-sample handling.

`teacher_review` stores every rating column as varchar (docs/current-state-audit
§2.4). Values that do not parse to 0–5 are **discarded, not clamped** — clamping
a stray "50" to 5 would manufacture a perfect score out of a data-entry error.

The other rule here is Bayesian shrinkage: a single 5★ review is not evidence
that a tutor is better than one with 4.6★ across forty reviews. Raw averages are
never used for ranking; `shrunk_rating` is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from tutor_match_meta.contracts.tutor import ReviewAggregate

RATING_MIN, RATING_MAX = 0.0, 5.0
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class RawReview:
    """One `teacher_review` row, before parsing."""

    rating: str | None = None
    expertise: str | None = None
    patience: str | None = None
    reliability: str | None = None
    communication: str | None = None
    date: str | None = None


def parse_rating(raw: str | None) -> float | None:
    """A rating in 0–5, or None. Out-of-range values are treated as malformed."""
    if raw is None:
        return None
    match = _NUMBER.search(str(raw))
    if not match:
        return None
    value = float(match.group())
    return value if RATING_MIN <= value <= RATING_MAX else None


def parse_review_date(raw: str | None) -> datetime | None:
    """`teacher_review.date` is free-text; try the formats the data uses."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def aggregate(reviews: Iterable[RawReview]) -> ReviewAggregate:
    """Aggregate published reviews into the projection shape.

    `count` is the number of rows that yielded at least one usable rating, not
    the number of rows fetched — so a tutor with ten unparseable rows does not
    look like a tutor with ten reviews.
    """
    overall: list[float] = []
    expertise: list[float] = []
    patience: list[float] = []
    reliability: list[float] = []
    communication: list[float] = []
    latest: datetime | None = None
    counted = 0

    for review in reviews:
        values = {
            "rating": parse_rating(review.rating),
            "expertise": parse_rating(review.expertise),
            "patience": parse_rating(review.patience),
            "reliability": parse_rating(review.reliability),
            "communication": parse_rating(review.communication),
        }
        if not any(v is not None for v in values.values()):
            continue
        counted += 1
        for key, bucket in (
            ("rating", overall),
            ("expertise", expertise),
            ("patience", patience),
            ("reliability", reliability),
            ("communication", communication),
        ):
            value = values[key]
            if value is not None:
                bucket.append(value)
        when = parse_review_date(review.date)
        if when and (latest is None or when > latest):
            latest = when

    return ReviewAggregate(
        count=counted,
        rating_avg=_mean(overall),
        expertise_avg=_mean(expertise),
        patience_avg=_mean(patience),
        reliability_avg=_mean(reliability),
        communication_avg=_mean(communication),
        latest_review_at=latest,
    )


def shrunk_rating(
    rating_avg: float | None, count: int, *, prior_mean: float, prior_weight: float
) -> float | None:
    """Bayesian-shrunk rating: `(n·x̄ + m·μ) / (n + m)`.

    `prior_weight` is the number of imaginary average reviews mixed in. At the
    default of 5, a lone 5★ review lands near 4.4 instead of beating a
    forty-review 4.8 — which is the entire point.

    Both parameters come from the scoring policy; nothing here is hardcoded.
    """
    if rating_avg is None or count <= 0:
        return None
    return round((count * rating_avg + prior_weight * prior_mean) / (count + prior_weight), 3)


def confidence_from_sample(count: int, *, full_confidence_at: int) -> float:
    """Confidence in a review-derived signal, from 0 at n=0 to ~1 at the target.

    Square-root growth: going from 1 to 4 reviews adds far more certainty than
    going from 30 to 33.
    """
    if count <= 0 or full_confidence_at <= 0:
        return 0.0
    return float(round(min(1.0, (count / full_confidence_at) ** 0.5), 3))


def recency_factor(latest: datetime | None, *, half_life_days: float, now: datetime) -> float:
    """Exponential decay on review age. 1.0 when fresh, 0.5 at one half-life.

    Floors at 0.25: an old review is weaker evidence, never zero evidence.
    """
    if latest is None or half_life_days <= 0:
        return 1.0
    age_days = max(0.0, (now - latest).total_seconds() / 86_400)
    return float(round(max(0.25, 0.5 ** (age_days / half_life_days)), 3))

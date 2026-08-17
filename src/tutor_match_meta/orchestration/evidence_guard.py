"""The anti-fabrication gate.

Everything a parent is told about a tutor passes through here first. The guard
answers exactly one question: **is this claim backed by evidence we actually
hold?**

It is a whitelist, not a filter. A claim is dropped unless it can name the
`SkillScore` and `Evidence` that support it — so the failure mode is saying too
little, never saying something untrue. A tutor with a thin profile produces a
short shortlist entry, which is correct.

This is what stops the explanation layer (and any model behind it) from turning
`DataQuality.MISSING` availability into "free on Monday evenings".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tutor_match_meta.contracts.common import (
    INTERNAL_ONLY_DIMENSIONS,
    DataQuality,
    Dimension,
    Evidence,
)
from tutor_match_meta.contracts.scoring import ScoredCandidate, SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.scoring.policy import ScoringPolicy


class ClaimKind(StrEnum):
    """The kinds of factual claim a shortlist entry can make."""

    NAME = "name"
    PROFILE_URL = "profile_url"
    SUBJECT = "subject"
    BOARD = "board"
    CLASS = "class"
    MODE = "mode"
    LOCALITY = "locality"
    DISTANCE = "distance"
    AVAILABILITY = "availability"
    FEE = "fee"
    RATING = "rating"
    EXPERIENCE = "experience"
    TEACHING_STYLE = "teaching_style"
    VERIFICATION = "verification"


#: Which dimension must vouch for each claim. A claim whose dimension is
#: unquotable (missing/insufficient/stale evidence) is refused.
_BACKING_DIMENSION: dict[ClaimKind, Dimension] = {
    ClaimKind.SUBJECT: Dimension.SUBJECT_EXPERTISE,
    ClaimKind.BOARD: Dimension.ACADEMIC,
    ClaimKind.CLASS: Dimension.ACADEMIC,
    ClaimKind.AVAILABILITY: Dimension.AVAILABILITY,
    ClaimKind.DISTANCE: Dimension.PROXIMITY,
    ClaimKind.LOCALITY: Dimension.PROXIMITY,
    ClaimKind.RATING: Dimension.PERFORMANCE,
    ClaimKind.EXPERIENCE: Dimension.PERFORMANCE,
    ClaimKind.TEACHING_STYLE: Dimension.PERSONALITY,
}

#: Claims backed by the projection row itself rather than by a scored dimension.
#: A tutor's published fee band is a stored fact; whether the *negotiation*
#: dimension had a budget to compare it against says nothing about whether the
#: fee is real. Gating the fee on that dimension hid published prices from
#: parents who simply had not stated a budget.
_PROJECTION_BACKED: frozenset[ClaimKind] = frozenset(
    {ClaimKind.NAME, ClaimKind.PROFILE_URL, ClaimKind.MODE, ClaimKind.FEE}
)

#: Claims that can never be made. The website has no verification flag beyond
#: `status='t'` (docs/current-state-audit.md §3), so "verified tutor" would be a
#: fabricated trust signal.
_FORBIDDEN_CLAIMS: frozenset[ClaimKind] = frozenset({ClaimKind.VERIFICATION})


@dataclass(frozen=True, slots=True)
class Claim:
    """A proposed statement about a tutor, with the evidence behind it."""

    kind: ClaimKind
    text: str
    evidence: tuple[Evidence, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.kind}:{self.text}"


@dataclass(frozen=True, slots=True)
class GuardResult:
    approved: tuple[Claim, ...]
    refused: tuple[tuple[Claim, str], ...]

    @property
    def refusal_reasons(self) -> list[str]:
        return [reason for _, reason in self.refused]


class EvidenceGuard:
    """Approves only claims that a scored dimension can vouch for."""

    def __init__(self, policy: ScoringPolicy) -> None:
        self._policy = policy

    def check(self, claim: Claim, candidate: ScoredCandidate) -> str | None:
        """Return a refusal reason, or None when the claim is safe to make."""
        if claim.kind in _FORBIDDEN_CLAIMS:
            return f"claim_kind_forbidden:{claim.kind}"

        # Projection-backed claims need no scored dimension to vouch for them;
        # the link resolver has already verified the row is live and canonical.
        if claim.kind in _PROJECTION_BACKED:
            return None if claim.text.strip() else "empty_claim"

        dimension = _BACKING_DIMENSION.get(claim.kind)
        if dimension is None:
            return f"no_backing_dimension:{claim.kind}"

        internal = dimension in INTERNAL_ONLY_DIMENSIONS
        if internal and not self._policy.explanation.expose_replacement_risk:
            return f"dimension_internal_only:{dimension}"

        score = candidate.scores.get(dimension)
        if score is None:
            return f"dimension_not_scored:{dimension}"
        if not score.quotable:
            return f"dimension_not_quotable:{dimension}:{score.data_quality}"
        if not claim.evidence and not score.evidence:
            return f"no_evidence:{dimension}"
        return None

    def filter(self, claims: list[Claim], candidate: ScoredCandidate) -> GuardResult:
        approved: list[Claim] = []
        refused: list[tuple[Claim, str]] = []
        for claim in claims:
            reason = self.check(claim, candidate)
            if reason is None:
                approved.append(claim)
            else:
                refused.append((claim, reason))
        return GuardResult(approved=tuple(approved), refused=tuple(refused))

    def quotable_dimensions(self, candidate: ScoredCandidate) -> list[Dimension]:
        """Citable dimensions, in the policy's preference order."""
        return [
            dimension
            for dimension in self._policy.explanation.citable_dimensions
            if dimension not in INTERNAL_ONLY_DIMENSIONS
            and (score := candidate.scores.get(dimension)) is not None
            and score.quotable
        ]


def assert_no_fabrication(
    entry_text: str, tutor: TutorCandidate, scores: dict[Dimension, SkillScore]
) -> list[str]:
    """Post-hoc audit of rendered text against what the data supports.

    A second, independent check on the *output string* rather than on the claim
    objects — because the failure this defends against is a future template (or
    a model-written sentence) that bypasses `Claim` entirely.

    Returns a list of violations; empty means clean.
    """
    violations: list[str] = []
    lowered = entry_text.lower()

    availability = scores.get(Dimension.AVAILABILITY)
    if _mentions_schedule(lowered) and (
        availability is None
        or availability.data_quality is DataQuality.MISSING
        or not tutor.has_availability_data
    ):
        violations.append("schedule_claimed_without_availability_data")

    performance = scores.get(Dimension.PERFORMANCE)
    if _mentions_rating(lowered) and (
        performance is None or not performance.quotable or tutor.reviews.count == 0
    ):
        violations.append("rating_claimed_without_review_data")

    if _mentions_distance(lowered) and tutor.geo is None:
        violations.append("distance_claimed_without_coordinates")

    if "verified" in lowered or "background check" in lowered:
        violations.append("verification_claimed_without_source")

    if _mentions_hourly_fee(lowered) and not tutor.fee.unit_known:
        violations.append("fee_unit_claimed_without_source")

    return violations


def _mentions_schedule(text: str) -> bool:
    day_words = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "mon ",
        "tue ",
        "wed ",
        "thu ",
        "fri ",
        "sat ",
        "sun ",
        "weekend",
        "weekday",
    )
    return any(word in text for word in day_words) or "available at" in text


def _mentions_rating(text: str) -> bool:
    return any(word in text for word in ("rated", "rating", "★", "star", "reviews"))


def _mentions_distance(text: str) -> bool:
    return " km" in text or "kilomet" in text or "minutes away" in text


def _mentions_hourly_fee(text: str) -> bool:
    return any(word in text for word in ("per hour", "/hr", "hourly", "per month", "monthly"))

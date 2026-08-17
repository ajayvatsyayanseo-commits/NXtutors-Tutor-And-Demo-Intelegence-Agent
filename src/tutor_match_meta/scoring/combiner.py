"""Combining eight skill scores into one ranked shortlist.

A dimension contributes `weight × confidence` at its score; **the weight it does
not claim is filled with the policy's neutral prior.** So the final score is
always normalised over the full 1.0, never over "whatever we happened to know".

That fill is the important part, and it was not obvious. The natural
implementation — average only the dimensions that had data — quietly *rewards*
missing data: a tutor known only through their two strongest dimensions averages
higher than a tutor measured on all eight, because the well-measured one has real
mid-range scores dragging their mean down. In testing that put a tutor with a
single 5-star review above one with seventeen reviews at 4.6.

Neutral fill makes the treatment symmetric: an unknown dimension neither helps
nor harms, it simply says nothing. `weight_coverage` is reported alongside so the
orchestrator can still escalate a shortlist built on too little evidence.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension
from tutor_match_meta.contracts.scoring import ScoredCandidate, SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.scoring.policy import ScoringPolicy


def combine(
    *,
    tutor: TutorCandidate,
    pseudonym: str,
    scores: dict[Dimension, SkillScore],
    policy: ScoringPolicy,
) -> ScoredCandidate:
    """Fold a full skill vector into a single ranked candidate."""
    weighted_total = 0.0
    contributing_weight = 0.0
    declared_weight = 0.0
    confidence_numerator = 0.0
    flags: list[str] = []

    for dimension, weight in policy.weights.items():
        declared_weight += weight
        score = scores.get(dimension)
        if score is None:
            flags.append(f"missing_dimension:{dimension}")
            continue
        if score.confidence < policy.thresholds.min_dimension_confidence:
            # Too uncertain to move the ranking. Excluded from the numerator AND
            # the denominator, so it neither helps nor hurts.
            continue
        effective = weight * score.confidence
        weighted_total += score.score * effective
        contributing_weight += effective
        confidence_numerator += weight * score.confidence
        flags.extend(f"{dimension}:{flag}" for flag in score.flags)

    neutral = policy.thresholds.neutral_score
    if declared_weight <= 0.0:  # pragma: no cover - policy validation forbids this
        final, coverage, confidence = neutral, 0.0, 0.0
    else:
        # Fill the unclaimed weight with the neutral prior rather than dropping
        # it from the denominator. See the module docstring: dropping it makes
        # ignorance look like excellence.
        unclaimed = max(0.0, declared_weight - contributing_weight)
        final = (weighted_total + unclaimed * neutral) / declared_weight
        coverage = contributing_weight / declared_weight
        confidence = confidence_numerator / declared_weight

    if contributing_weight <= 0.0:
        flags.append("no_usable_dimensions")

    if coverage < policy.thresholds.min_weight_coverage:
        flags.append("low_weight_coverage")
    if any(s.data_quality is DataQuality.STALE for s in scores.values()):
        flags.append("stale_evidence")

    return ScoredCandidate(
        tutor_id=tutor.tutor_id,
        public_ref=tutor.public_ref,
        pseudonym=pseudonym,
        scores=scores,
        final_score=round(min(1.0, max(0.0, final)), 4),
        weight_coverage=round(min(1.0, coverage), 4),
        confidence=round(min(1.0, confidence), 4),
        freshness=tutor.freshness,
        flags=tuple(dict.fromkeys(flags)),
    )


def rank(
    candidates: list[ScoredCandidate],
    *,
    policy: ScoringPolicy,
    locality_of: dict[str, str | None],
) -> list[ScoredCandidate]:
    """Order candidates and apply the diversity rule.

    Sorting is fully deterministic — score, then confidence, then coverage, then
    tutor id. Without the final tiebreak, two equally-scored tutors would swap
    places between runs and the same parent would see a different "best" tutor
    on a retry.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (-c.final_score, -c.confidence, -c.weight_coverage, c.tutor_id),
    )
    if not policy.diversity.enabled:
        return ordered
    return _diversify(ordered, policy=policy, locality_of=locality_of)


def _diversify(
    ordered: list[ScoredCandidate],
    *,
    policy: ScoringPolicy,
    locality_of: dict[str, str | None],
) -> list[ScoredCandidate]:
    """Prefer variety only among candidates of comparable quality.

    Three tutors from the same apartment complex is a worse shortlist than two
    from there and one from the next sector — but only when the third is
    genuinely close in score. A candidate is deferred, never dropped: if nothing
    more diverse is good enough, the deferred one is still used.
    """
    limit = policy.diversity.max_same_locality
    sacrifice = policy.diversity.max_score_sacrifice
    target = policy.thresholds.shortlist_size

    chosen: list[ScoredCandidate] = []
    deferred: list[ScoredCandidate] = []
    counts: dict[str, int] = {}

    for candidate in ordered:
        if len(chosen) >= target:
            deferred.append(candidate)
            continue
        locality = (locality_of.get(candidate.tutor_id) or "").lower() or "_unknown"
        if locality != "_unknown" and counts.get(locality, 0) >= limit:
            # Only defer when a comparable alternative could plausibly replace
            # it; otherwise diversity would cost real quality.
            best_remaining = max(
                (c.final_score for c in ordered if c not in chosen and c is not candidate),
                default=0.0,
            )
            if candidate.final_score - best_remaining <= sacrifice:
                deferred.append(candidate)
                continue
        chosen.append(candidate)
        counts[locality] = counts.get(locality, 0) + 1

    # Backfill from the deferred pool, best first, so diversity never shrinks
    # the shortlist below what the score threshold would have allowed.
    for candidate in deferred:
        if len(chosen) >= target:
            break
        chosen.append(candidate)

    remaining = [c for c in ordered if c not in chosen]
    return chosen + sorted(remaining, key=lambda c: (-c.final_score, -c.confidence, c.tutor_id))


def shortlist_cutoff(
    ranked: list[ScoredCandidate], *, policy: ScoringPolicy
) -> list[ScoredCandidate]:
    """Take the top N, dropping anything under the quality bar.

    The bar is absolute (docs/assumptions.md A17): returning two strong matches,
    or one, or none, is correct. Padding to three with a tutor who does not fit
    is how a matcher loses a parent's trust permanently.
    """
    return [c for c in ranked if c.final_score >= policy.thresholds.min_final_score][
        : policy.thresholds.shortlist_size
    ]

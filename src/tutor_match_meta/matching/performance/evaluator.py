"""Agent 015 — Tutor Past Performance Score.

Skills: aggregate reviews · normalise ratings · compute retention · score
outcomes · flag risk.

Everything here is grounded in `teacher_review`, the only outcome evidence the
website holds. Three rules keep it honest:

1. **Shrinkage.** A raw average is never used. One 5★ review shrinks toward the
   policy prior, so it cannot outrank a forty-review 4.8★.
2. **A minimum sample.** Below `min_reviews_for_rating` the dimension reports
   `INSUFFICIENT`, and the explanation layer may not cite a rating at all.
3. **Recency decay.** A five-year-old review is weaker evidence than a recent
   one, floored rather than zeroed.

Retention and demo-continuation are listed skills but have no source data today
(docs/assumptions.md A10); the hooks exist and report `MISSING` until this
service accumulates its own outcome records.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import reviews as review_domain
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence

#: Experience beyond this stops adding to the score.
_EXPERIENCE_SATURATION_YEARS = 12
#: Weight given to experience when there are too few reviews to say anything.
_EXPERIENCE_ONLY_CONFIDENCE = 0.35


class PerformanceEvaluator:
    """Verified review outcomes, shrunk and recency-weighted."""

    dimension = Dimension.PERFORMANCE

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        policy = context.policy.reviews
        aggregate = tutor.reviews
        neutral = context.policy.thresholds.neutral_score

        if aggregate.count == 0:
            # No reviews at all. Fall back to experience, which is weak but real,
            # and be explicit that this is not an outcome signal.
            return self._experience_only(tutor, context, reason="no_reviews")

        if aggregate.count < policy.min_reviews_for_rating:
            score = self._experience_score(tutor)
            return build(
                self.dimension,
                score=score if score is not None else neutral,
                confidence=0.25,
                quality=DataQuality.INSUFFICIENT,
                evidences=(evidence("website.teacher_review", "count", aggregate.count),),
                flags=(f"low_review_sample:{aggregate.count}",),
                reasons=("below_min_review_sample",),
            )

        shrunk = review_domain.shrunk_rating(
            aggregate.rating_avg,
            aggregate.count,
            prior_mean=policy.prior_mean,
            prior_weight=policy.prior_weight,
        )
        if shrunk is None:
            return self._experience_only(tutor, context, reason="ratings_unparseable")

        recency = review_domain.recency_factor(
            aggregate.latest_review_at,
            half_life_days=policy.recency_half_life_days,
            now=context.now,
        )
        sample_confidence = review_domain.confidence_from_sample(
            aggregate.count, full_confidence_at=policy.full_confidence_at
        )

        # Rating on a 0-5 scale becomes a 0-1 score. Recency pulls a stale
        # reputation back toward the prior rather than scaling the score to zero
        # — an old good tutor is not a bad tutor.
        rating_score = clamp(shrunk / 5.0)
        prior_score = clamp(policy.prior_mean / 5.0)
        score = prior_score + (rating_score - prior_score) * recency

        reliability = self._reliability_component(tutor)
        if reliability is not None:
            # Reliability is the sub-score that best predicts an arrangement
            # surviving, so it gets real weight alongside the headline rating.
            score = 0.75 * score + 0.25 * reliability

        evidences: list[Evidence] = [
            evidence(
                "website.teacher_review",
                "rating_avg",
                f"{aggregate.rating_avg:.1f}" if aggregate.rating_avg else "n/a",
                aggregate.latest_review_at,
            ),
            evidence("website.teacher_review", "count", aggregate.count),
        ]
        flags: list[str] = []
        reasons: list[str] = [f"shrunk_rating:{shrunk:.2f}", f"recency:{recency:.2f}"]

        quality = DataQuality.OK
        if recency < 0.5:
            quality = DataQuality.STALE
            flags.append("reviews_stale")
        if aggregate.count < policy.full_confidence_at:
            flags.append(f"modest_review_sample:{aggregate.count}")

        confidence = clamp(0.4 + 0.5 * sample_confidence)

        return build(
            self.dimension,
            score=score,
            confidence=confidence,
            quality=quality,
            evidences=tuple(evidences),
            flags=tuple(flags),
            reasons=tuple(reasons),
        )

    # ------------------------------------------------------------- internals
    def _reliability_component(self, tutor: TutorCandidate) -> float | None:
        value = tutor.reviews.reliability_avg
        return clamp(value / 5.0) if value is not None else None

    def _experience_score(self, tutor: TutorCandidate) -> float | None:
        if tutor.experience_years is None:
            return None
        # Deliberately compressed into the middle of the range: experience is a
        # proxy for competence, not proof of it, so it can never produce a
        # top-of-range score on its own.
        return 0.45 + 0.3 * clamp(tutor.experience_years / _EXPERIENCE_SATURATION_YEARS)

    def _experience_only(
        self, tutor: TutorCandidate, context: EvaluationContext, *, reason: str
    ) -> SkillScore:
        score = self._experience_score(tutor)
        if score is None:
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=(reason, "no_experience_data"),
                flags=("no_outcome_evidence",),
            )
        return build(
            self.dimension,
            score=score,
            confidence=_EXPERIENCE_ONLY_CONFIDENCE,
            quality=DataQuality.INSUFFICIENT,
            evidences=(
                evidence("website.register", "experience", f"{tutor.experience_years} years"),
            ),
            flags=("no_review_evidence",),
            reasons=(reason, "experience_proxy"),
        )

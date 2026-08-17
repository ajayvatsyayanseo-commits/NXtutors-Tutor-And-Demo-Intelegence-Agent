"""Agent 022 — Tutor Replacement Risk.

Skills: detect churn signals · score dissatisfaction · analyse conflict pattern ·
raise early alerts · suggest backup tutor.

**This dimension is internal-only.** Its reasoning never reaches a parent
(`INTERNAL_ONLY_DIMENSIONS`), and the policies weight it at 0.02 — enough to
break a tie between otherwise-equal candidates, never enough to decide a
shortlist.

That is not timidity, it is calibration: the website holds no churn history, no
replacement records and no cancellation data (docs/assumptions.md A10). Until
this service accumulates its own outcome records, the only grounded signals are
the `reliability` review sub-score and a low-rating pattern. An evaluator that
manufactured a confident risk narrative out of that would be inventing.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import reviews as review_domain
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence

#: Reliability sub-score below which an arrangement is materially more likely to
#: need replacing. Reviews are 1-5; 3.0 is "noticeably below par", not "bad".
_RELIABILITY_CONCERN = 3.0
#: Minimum reviews before a reliability read is worth acting on at all.
_MIN_REVIEWS = 3
#: Workload above which a tutor is statistically more likely to drop a student.
_OVERCOMMITTED_MULTIPLIER = 1.5


class ReplacementRiskEvaluator:
    """Internal reliability risk. Scores 1.0 = low risk, 0.0 = high risk."""

    dimension = Dimension.REPLACEMENT_RISK

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        aggregate = tutor.reviews
        neutral = context.policy.thresholds.neutral_score

        signals: list[tuple[float, float]] = []
        evidences: list[Evidence] = []
        flags: list[str] = []
        reasons: list[str] = []

        # ------------------------------------------------ reliability evidence
        if aggregate.count >= _MIN_REVIEWS and aggregate.reliability_avg is not None:
            reliability = aggregate.reliability_avg
            confidence_in_sample = review_domain.confidence_from_sample(
                aggregate.count, full_confidence_at=context.policy.reviews.full_confidence_at
            )
            signals.append((clamp(reliability / 5.0), 0.6 * max(0.4, confidence_in_sample)))
            evidences.append(
                evidence(
                    "website.teacher_review",
                    "reliability_avg",
                    f"{reliability:.1f}",
                    aggregate.latest_review_at,
                )
            )
            if reliability < _RELIABILITY_CONCERN:
                flags.append("low_reliability_signal")
                reasons.append(f"reliability_below_par:{reliability:.1f}")
            else:
                reasons.append(f"reliability_ok:{reliability:.1f}")

        # ------------------------------------------------ dissatisfaction trend
        if aggregate.count >= _MIN_REVIEWS and aggregate.rating_avg is not None:
            if aggregate.rating_avg < 3.0:
                signals.append((0.2, 0.3))
                flags.append("low_overall_rating")
                reasons.append("dissatisfaction_pattern")
            evidences.append(
                evidence("website.teacher_review", "rating_avg", f"{aggregate.rating_avg:.1f}")
            )

        # ------------------------------------------------------------ workload
        overload_threshold = context.policy.negotiation.overload_active_students
        if tutor.active_students is not None:
            limit = overload_threshold * _OVERCOMMITTED_MULTIPLIER
            if tutor.active_students > limit:
                signals.append((0.3, 0.25))
                flags.append("overcommitted")
                reasons.append(f"active_students:{tutor.active_students}")
                evidences.append(
                    evidence("tmm.tutor_workload", "active_students", tutor.active_students)
                )

        # --------------------------------------------------------- stale record
        if tutor.freshness.value == "aging":
            flags.append("projection_aging")

        if not signals:
            # The normal case today. Explicitly insufficient, tiny influence.
            return build(
                self.dimension,
                score=neutral,
                confidence=0.1,
                quality=DataQuality.INSUFFICIENT,
                evidences=tuple(evidences),
                flags=tuple(flags),
                reasons=("no_churn_evidence_available",),
            )

        total_weight = sum(w for _, w in signals)
        score = sum(s * w for s, w in signals) / total_weight

        return build(
            self.dimension,
            score=score,
            confidence=min(0.5, total_weight),
            quality=DataQuality.PARTIAL,
            evidences=tuple(evidences),
            flags=tuple(flags),
            reasons=tuple(reasons),
        )

    def needs_backup(self, score: SkillScore, *, threshold: float = 0.4) -> bool:
        """Whether a coordinator should line up a backup tutor.

        Requires real evidence: an `INSUFFICIENT` score never triggers a backup,
        because "we know nothing" is not the same as "this looks risky", and
        raising an alert on absent data trains coordinators to ignore alerts.
        """
        return score.data_quality is not DataQuality.INSUFFICIENT and score.score < threshold

"""Agent 021 — Tutor Negotiation Profile.

Skills: analyse fee history · score flexibility · model minimum fee · detect
overload risk · suggest negotiation style.

Three hard ethical constraints shape this module (docs/assumptions.md A12):

1. **No personalised pricing.** Nothing here reads, infers or stores a parent's
   willingness to pay. The only inputs are the parent's *stated* budget and the
   tutor's *stored* fee band.
2. **The strategy set is closed.** Suggestions come from the policy's approved
   list; this module selects, it never composes a new tactic.
3. **The tutor's floor is respected.** A gap that would push a tutor below their
   stated minimum requires human approval — the agent cannot grant it.

And the unit rule: `register.budget` has no unit, so a tutor fee is never
compared against a parent's per-hour figure as if the two were the same number.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence, scaled
from tutor_match_meta.scoring.policy import NegotiationStrategy


class NegotiationEvaluator:
    """Budget fit, evidence-based flexibility, and an approved strategy."""

    dimension = Dimension.NEGOTIATION

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        policy = context.policy.negotiation
        budget = context.requirement.budget
        fee = tutor.fee
        neutral = context.policy.thresholds.neutral_score

        if not budget:
            # No stated budget. Workload is still a real signal about whether
            # this tutor can take another student on reasonable terms.
            return self._workload_only(tutor, context)

        if not fee:
            return build(
                self.dimension,
                score=neutral,
                confidence=0.15,
                quality=DataQuality.MISSING,
                reasons=("tutor_fee_unknown",),
                flags=("fee_not_published",),
            )

        evidences: list[Evidence] = [
            evidence("website.register", "budget", fee.label or "", tutor.source_updated_at)
        ]
        flags: list[str] = []
        reasons: list[str] = []

        # The tutor's floor versus the parent's ceiling. Both may be absent;
        # only compare what actually exists.
        tutor_floor = fee.minimum
        parent_ceiling = budget.maximum

        if tutor_floor is None or parent_ceiling is None:
            score = neutral
            reasons.append("incomplete_fee_comparison")
            quality = DataQuality.PARTIAL
            confidence = 0.3
        else:
            ratio = tutor_floor / parent_ceiling if parent_ceiling > 0 else float("inf")
            score, ratio_reason = self._ratio_score(ratio, context)
            reasons.append(ratio_reason)
            reasons.append(f"fee_ratio:{ratio:.2f}")
            quality = DataQuality.OK
            confidence = 0.8

            if ratio > policy.max_over_budget_ratio:
                flags.append("beyond_negotiable_range")
            elif ratio > 1.0:
                strategy = policy.strategy_for(ratio)
                if strategy is not None:
                    reasons.append(f"strategy:{strategy.code}")
                    if strategy.requires_approval:
                        flags.append("requires_fee_approval")

        # A tutor already at capacity has little room to negotiate anything.
        overload = self._overload_penalty(tutor, context)
        if overload > 0:
            score = clamp(score - overload)
            flags.append("tutor_workload_high")
            reasons.append("overload_risk")
            evidences.append(
                evidence("tmm.tutor_workload", "active_students", tutor.active_students or 0)
            )

        # The unit mismatch is a real limit on how much this comparison means.
        if not fee.unit_known:
            flags.append("fee_unit_unknown")
            confidence = min(confidence, 0.6)
            quality = DataQuality.PARTIAL if quality is DataQuality.OK else quality

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
    def _ratio_score(self, ratio: float, context: EvaluationContext) -> tuple[float, str]:
        policy = context.policy.negotiation
        if ratio <= 1.0:
            return policy.within_budget_score, "within_budget"
        if ratio > policy.max_over_budget_ratio:
            return 0.0, "beyond_negotiable_range"
        # Linear decay across the negotiable band: at the edge of what an
        # approved strategy can close, the fit is poor but not disqualifying.
        return scaled(ratio, best=1.0, worst=policy.max_over_budget_ratio), "negotiable_gap"

    def _overload_penalty(self, tutor: TutorCandidate, context: EvaluationContext) -> float:
        threshold = context.policy.negotiation.overload_active_students
        if tutor.active_students is None or tutor.active_students <= threshold:
            return 0.0
        excess = tutor.active_students - threshold
        return min(0.3, 0.05 * excess)

    def _workload_only(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        if tutor.active_students is None:
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("no_budget_stated", "no_workload_data"),
            )
        penalty = self._overload_penalty(tutor, context)
        return build(
            self.dimension,
            score=clamp(0.7 - penalty),
            confidence=0.3,
            quality=DataQuality.PARTIAL,
            evidences=(evidence("tmm.tutor_workload", "active_students", tutor.active_students),),
            flags=("tutor_workload_high",) if penalty > 0 else (),
            reasons=("no_budget_stated", "workload_only"),
        )

    def strategy_for(
        self, tutor: TutorCandidate, context: EvaluationContext
    ) -> NegotiationStrategy | None:
        """The approved strategy for this pairing, or None if none is needed.

        Returns None both when there is no gap and when the gap is beyond what
        any approved strategy covers — the caller distinguishes via the
        `beyond_negotiable_range` flag rather than by receiving a made-up tactic.
        """
        budget, fee = context.requirement.budget, tutor.fee
        if not budget or not fee or fee.minimum is None or budget.maximum is None:
            return None
        if budget.maximum <= 0:
            return None
        ratio = fee.minimum / budget.maximum
        if ratio <= 1.0:
            return context.policy.negotiation.strategy_for(1.0)
        return context.policy.negotiation.strategy_for(ratio)

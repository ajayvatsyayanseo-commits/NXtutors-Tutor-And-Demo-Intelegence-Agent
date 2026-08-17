"""Agent 016 — Tutor Personality Compatibility.

Skills: profile communication · map temperament · score compatibility · predict
conflict · suggest pairing.

Scoped down deliberately. "Map temperament" is implemented as **teaching-style
and communication-style matching**, not personality profiling: the evidence
allowlist in `evidence.py` is the only source of traits, and it contains nothing
that could express a psychological or protected attribute.

Two independent signals combine:

* **declared style overlap** — what the parent asked for versus what the tutor
  says about their own approach;
* **verified communication evidence** — the `patience` and `communication`
  sub-scores from published reviews.

When neither exists, this returns MISSING and contributes nothing. It carries the
second-smallest weight in every policy for exactly that reason.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import reviews as review_domain
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence
from tutor_match_meta.matching.personality.evidence import (
    REVIEW_BACKED,
    StyleTrait,
    conflicts_between,
    traits_from_profile,
    traits_from_request,
)

#: Minimum reviews before a sub-score is treated as communication evidence.
#: Lower than the headline-rating bar: a sub-score is used as a nudge here, not
#: quoted as a statistic to the parent.
_MIN_SUBSCORE_REVIEWS = 2


class PersonalityCompatibilityEvaluator:
    """Teaching-style fit from declared approach and verified review sub-scores."""

    dimension = Dimension.PERSONALITY

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        requirement = context.requirement
        neutral = context.policy.thresholds.neutral_score

        wanted = traits_from_request(requirement.value_of("preferred_teaching_style"))
        # A parent's learning goal and weak topics also state style needs:
        # "struggling in physics" is a request for beginner-friendly patience.
        wanted |= traits_from_request(requirement.value_of("learning_goal"))
        wanted |= traits_from_request(" ".join(requirement.weak_topics))

        offered = traits_from_profile(tutor.profile_summary)
        subscores = self._review_traits(tutor)

        if not wanted and not offered and not subscores:
            return build(
                self.dimension,
                score=neutral,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("no_style_signals",),
            )

        evidences: list[Evidence] = []
        flags: list[str] = []
        reasons: list[str] = []
        components: list[tuple[float, float]] = []

        # --------------------------------------------- declared style overlap
        if wanted:
            if offered:
                matched = wanted & offered
                overlap = len(matched) / len(wanted)
                components.append((overlap, 0.6))
                reasons.append(f"style_overlap:{len(matched)}/{len(wanted)}")
                if matched:
                    evidences.append(
                        evidence(
                            "website.register",
                            "profile_desc",
                            ", ".join(sorted(t.value for t in matched)),
                            tutor.source_updated_at,
                        )
                    )
            else:
                flags.append("tutor_style_undeclared")

            clashes = conflicts_between(wanted, offered)
            if clashes:
                flags.append(
                    "style_conflict:" + ",".join(f"{a.value}~{b.value}" for a, b in clashes[:2])
                )
                reasons.append("predicted_style_conflict")
                components.append((0.0, 0.25))
        else:
            reasons.append("no_style_requested")

        # -------------------------------------- verified communication evidence
        for trait, value in subscores.items():
            weight = 0.4 if trait in wanted else 0.2
            components.append((clamp(value / 5.0), weight))
            evidences.append(
                evidence(
                    "website.teacher_review",
                    REVIEW_BACKED[trait],
                    f"{value:.1f}",
                    tutor.reviews.latest_review_at,
                )
            )
            reasons.append(f"review_{trait.value}:{value:.1f}")

        if not components:
            return build(
                self.dimension,
                score=neutral,
                confidence=0.15,
                quality=DataQuality.INSUFFICIENT,
                flags=tuple(flags),
                reasons=tuple(reasons) or ("insufficient_style_evidence",),
            )

        total_weight = sum(w for _, w in components)
        score = sum(s * w for s, w in components) / total_weight

        quality = DataQuality.OK if (subscores and evidences) else DataQuality.PARTIAL
        confidence = 0.6 if subscores else 0.35

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
    def _review_traits(self, tutor: TutorCandidate) -> dict[StyleTrait, float]:
        """Traits corroborated by review sub-scores, if the sample allows."""
        if tutor.reviews.count < _MIN_SUBSCORE_REVIEWS:
            return {}
        found: dict[StyleTrait, float] = {}
        for trait, attribute in REVIEW_BACKED.items():
            value = getattr(tutor.reviews, attribute, None)
            if value is not None:
                found[trait] = float(value)
        return found

    def suggest_pairing(self, tutor: TutorCandidate, context: EvaluationContext) -> str | None:
        """A short, non-clinical pairing note for the coordinator.

        Returns None rather than a generic platitude when there is nothing
        evidence-backed to say.
        """
        wanted = traits_from_request(context.requirement.value_of("preferred_teaching_style"))
        offered = traits_from_profile(tutor.profile_summary)
        matched = sorted((wanted & offered), key=lambda t: t.value)
        if matched:
            styles = ", ".join(t.value.replace("_", " ") for t in matched)
            return f"matches requested approach: {styles}"
        patience = tutor.reviews.patience_avg
        if patience is not None and tutor.reviews.count >= _MIN_SUBSCORE_REVIEWS:
            confidence = review_domain.confidence_from_sample(
                tutor.reviews.count, full_confidence_at=20
            )
            if confidence >= 0.4 and patience >= 4.0:
                return "reviews consistently mention patience"
        return None

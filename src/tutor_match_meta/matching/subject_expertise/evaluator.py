"""Agent 014 — Tutor Subject Expertise.

Skills: map subjects · align syllabus · tag depth level · score expertise ·
flag mismatches.

The depth model is the interesting part. "Teaches Maths" is not one capability —
Class 4 Maths and Class 12 Calculus are different jobs. Depth is inferred from
the highest class the tutor declares, their education, and their years of
experience, then compared against the depth the requirement actually needs.
"""

from __future__ import annotations

from enum import IntEnum

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import academics, subjects
from tutor_match_meta.domain.text import normalize_key
from tutor_match_meta.matching.base import EvaluationContext, build, clamp, evidence


class Depth(IntEnum):
    """How deep the subject knowledge needs to go. Ordered, so it compares."""

    FOUNDATION = 1  # up to Class 5
    INTERMEDIATE = 2  # Class 6-8
    ADVANCED = 3  # Class 9-10 / board
    SPECIALIST = 4  # Class 11-12, JEE/NEET/IB HL


#: Postgraduate/doctoral markers in `register.education`, which is free text.
_HIGH_EDUCATION = ("phd", "mphil", "postgrad", "masters", "msc", "ma", "mtech", "mba", "med")
_TEACHING_QUALIFICATION = ("bed", "med", "ctet", "tet", "netqualified", "net")

#: Experience beyond this adds nothing to the depth signal — a 25-year veteran
#: and a 12-year veteran are both simply experienced.
_EXPERIENCE_SATURATION = 12


class SubjectExpertiseEvaluator:
    """Subject coverage × required depth."""

    dimension = Dimension.SUBJECT_EXPERTISE

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        requirement = context.requirement
        required = requirement.all_subjects

        if not required:
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("no_subject_requested",),
            )
        if not tutor.capabilities.subjects:
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("tutor_subjects_unknown",),
                flags=("tutor_subjects_unknown",),
            )

        evidences: list[Evidence] = [
            evidence(
                "website.teacher_courses",
                "subject",
                ", ".join(tutor.capabilities.subjects[:5]),
                tutor.source_updated_at,
            )
        ]
        flags: list[str] = []
        reasons: list[str] = []

        coverage, missing = self._coverage(required, tutor)
        if missing:
            flags.append("subject_gap:" + ",".join(missing[:3]))
            reasons.append(f"subjects_missing:{len(missing)}/{len(required)}")
        else:
            reasons.append("subjects_all_covered")

        required_depth = self._required_depth(context)
        tutor_depth, depth_evidence = self._tutor_depth(tutor)
        evidences.extend(depth_evidence)

        depth_score = self._depth_score(required_depth, tutor_depth)
        reasons.append(f"depth:{tutor_depth.name.lower()}_vs_{required_depth.name.lower()}")
        if tutor_depth < required_depth:
            flags.append(f"depth_shortfall:{required_depth.name.lower()}")

        # Coverage dominates: a tutor who does not teach the subject at all
        # cannot be rescued by having taught something else deeply.
        score = 0.7 * coverage + 0.3 * depth_score

        adjacency = self._adjacency_bonus(required, tutor)
        if adjacency > 0:
            score = clamp(score + adjacency)
            reasons.append("adjacent_subject_support")

        quality = DataQuality.OK if coverage > 0 else DataQuality.PARTIAL
        confidence = 0.85 if tutor.capabilities.subjects and coverage > 0 else 0.5

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
    def _coverage(
        self, required: tuple[str, ...], tutor: TutorCandidate
    ) -> tuple[float, list[str]]:
        """Fraction of requested subjects the tutor genuinely teaches."""
        taught = list(tutor.capabilities.subjects)
        missing = [s for s in required if not subjects.matches_any(s, taught)]
        return (len(required) - len(missing)) / len(required), missing

    def _adjacency_bonus(self, required: tuple[str, ...], tutor: TutorCandidate) -> float:
        """Small credit for teaching subjects commonly paired with the request.

        A Physics request answered by a Physics+Maths tutor is genuinely better
        for a science student. Capped low — it is a nicety, not a qualification.
        """
        taught = list(tutor.capabilities.subjects)
        pairs = sum(1 for want in required for have in taught if subjects.is_adjacent(want, have))
        return min(0.05, 0.025 * pairs)

    def _required_depth(self, context: EvaluationContext) -> Depth:
        requirement = context.requirement
        exam = academics.normalize_exam(requirement.value_of("exam"))
        if exam in (academics.JEE, academics.NEET, academics.OLYMPIAD):
            return Depth.SPECIALIST
        grade = academics.class_number(requirement.value_of("student_class"))
        if grade is None:
            return Depth.INTERMEDIATE
        if grade <= 5:
            return Depth.FOUNDATION
        if grade <= 8:
            return Depth.INTERMEDIATE
        if grade <= 10:
            return Depth.ADVANCED
        return Depth.SPECIALIST

    def _tutor_depth(self, tutor: TutorCandidate) -> tuple[Depth, list[Evidence]]:
        """Infer depth from the highest class taught, education and experience."""
        evidences: list[Evidence] = []
        grades = [n for c in tutor.capabilities.classes if (n := academics.class_number(c))]
        highest = max(grades) if grades else None

        if highest is None:
            depth = Depth.INTERMEDIATE
        elif highest >= 11:
            depth = Depth.SPECIALIST
        elif highest >= 9:
            depth = Depth.ADVANCED
        elif highest >= 6:
            depth = Depth.INTERMEDIATE
        else:
            depth = Depth.FOUNDATION

        if highest is not None:
            evidences.append(
                evidence("website.teacher_courses", "highest_class", f"Class {highest}")
            )

        education_key = normalize_key(tutor.education or "")
        if education_key and any(marker in education_key for marker in _HIGH_EDUCATION):
            # A postgraduate qualification lifts depth by one band, capped at
            # specialist — but never substitutes for having taught the level.
            depth = Depth(min(int(Depth.SPECIALIST), int(depth) + 1))
            evidences.append(evidence("website.register", "education", tutor.education or ""))
        elif education_key and any(m in education_key for m in _TEACHING_QUALIFICATION):
            evidences.append(evidence("website.register", "education", tutor.education or ""))

        return depth, evidences

    def _depth_score(self, required: Depth, actual: Depth) -> float:
        """1.0 when the tutor meets or exceeds the needed depth.

        Over-qualification is *slightly* penalised, not rewarded: a JEE
        specialist teaching Class 4 addition tends to pitch wrong and to churn.
        """
        if actual == required:
            return 1.0
        if actual > required:
            return max(0.75, 1.0 - 0.12 * (int(actual) - int(required)))
        return max(0.0, 1.0 - 0.4 * (int(required) - int(actual)))

    def experience_factor(self, tutor: TutorCandidate) -> float:
        """0-1 experience signal, saturating. Exposed for the performance skill."""
        if tutor.experience_years is None:
            return 0.5
        return clamp(tutor.experience_years / _EXPERIENCE_SATURATION)

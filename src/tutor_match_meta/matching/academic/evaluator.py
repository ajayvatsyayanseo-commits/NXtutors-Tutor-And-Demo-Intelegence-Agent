"""Agent 017 — Tutor Academic Compatibility.

Skills: match board · match class · match exam · map topic coverage · rate
academic fit.

Distinct from subject expertise (014): this asks "can this tutor teach *this
student's* syllabus at *this* level", not "how deeply do they know the subject".
A brilliant Physics postgrad who has only ever taught Class 11-12 IB is a poor
academic fit for a Class 6 CBSE student, and that is what this dimension catches.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import academics
from tutor_match_meta.matching.base import EvaluationContext, build, evidence

#: Grades away from a tutor's declared range at which fit reaches zero. Teaching
#: one grade outside a declared range is routine; four is a different job.
_GRADE_TOLERANCE = 4


class AcademicCompatibilityEvaluator:
    """Board × class × exam × level fit."""

    dimension = Dimension.ACADEMIC

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        requirement = context.requirement
        parts: list[tuple[float, float]] = []  # (component score, component weight)
        evidences: list[Evidence] = []
        flags: list[str] = []
        reasons: list[str] = []

        required_class = requirement.value_of("student_class")
        required_board = requirement.value_of("board")
        required_exam = academics.normalize_exam(requirement.value_of("exam"))
        grade = academics.class_number(required_class)

        # ---------------------------------------------------------- class fit
        if required_class and tutor.capabilities.classes:
            class_score, class_reason = self._class_fit(required_class, tutor)
            parts.append((class_score, 0.45))
            reasons.append(class_reason)
            evidences.append(
                evidence(
                    "website.teacher_courses",
                    "for_class",
                    ", ".join(tutor.capabilities.classes[:4]),
                    tutor.source_updated_at,
                )
            )
        elif required_class:
            flags.append("tutor_classes_unknown")

        # ---------------------------------------------------------- board fit
        if required_board and tutor.capabilities.boards:
            board_score, board_reason = self._board_fit(required_board, tutor, grade)
            parts.append((board_score, 0.40))
            reasons.append(board_reason)
            evidences.append(
                evidence(
                    "website.teacher_courses",
                    "board",
                    ", ".join(tutor.capabilities.boards[:4]),
                    tutor.source_updated_at,
                )
            )
        elif required_board:
            flags.append("tutor_boards_unknown")

        # ----------------------------------------------------------- exam fit
        if required_exam:
            exam_score, exam_reason = self._exam_fit(required_exam, tutor)
            parts.append((exam_score, 0.15))
            reasons.append(exam_reason)

        if not parts:
            # Nothing to compare. Either the parent stated no academic
            # constraints or the tutor declared no capabilities.
            return build(
                self.dimension,
                score=context.policy.thresholds.neutral_score,
                confidence=0.1,
                quality=DataQuality.MISSING,
                reasons=("no_academic_signals",),
                flags=tuple(flags),
            )

        total_weight = sum(w for _, w in parts)
        score = sum(s * w for s, w in parts) / total_weight

        # Confidence tracks how much of the academic picture we could actually
        # compare — a class-only comparison is a partial answer.
        coverage = total_weight / 1.0
        quality = DataQuality.OK if coverage >= 0.8 and evidences else DataQuality.PARTIAL
        if not evidences:
            quality = DataQuality.PARTIAL

        return build(
            self.dimension,
            score=score,
            confidence=0.55 + 0.4 * coverage,
            quality=quality,
            evidences=tuple(evidences),
            flags=tuple(flags),
            reasons=tuple(reasons),
        )

    # ------------------------------------------------------------- internals
    def _class_fit(self, required_class: str, tutor: TutorCandidate) -> tuple[float, str]:
        if academics.teaches_class(required_class, tutor.capabilities.classes):
            return 1.0, "class_exact"

        want = academics.class_number(required_class)
        if want is None:
            return 0.5, "class_unparsed"

        taught = [n for c in tutor.capabilities.classes if (n := academics.class_number(c))]
        if not taught:
            return 0.5, "class_unparsed"

        # Partial credit by proximity: a Class 9 tutor asked for Class 10 is a
        # near miss; asked for Class 4 they are not.
        distance = min(abs(want - n) for n in taught)
        if distance >= _GRADE_TOLERANCE:
            return 0.0, f"class_far:{distance}"

        # Crossing the Class 10/11 boundary is a bigger jump than the raw grade
        # distance suggests — the syllabus and the stream both change.
        crosses_senior_boundary = any((want > 10) != (n > 10) for n in taught)
        penalty = 0.35 if crosses_senior_boundary else 0.0
        score = max(0.0, 1.0 - distance / _GRADE_TOLERANCE - penalty)
        return score, f"class_near:{distance}"

    def _board_fit(
        self, required_board: str, tutor: TutorCandidate, grade: int | None
    ) -> tuple[float, str]:
        want = academics.normalize_board(required_board)
        taught = {academics.normalize_board(b) for b in tutor.capabilities.boards}
        if want in taught:
            return 1.0, "board_exact"

        # A board mismatch that is not mandatory still costs, but how much
        # depends on whether the syllabuses actually diverge at this grade.
        if academics.board_is_mandatory(want, grade):
            # Should already have been hard-filtered; scoring 0 here is a
            # defence in depth rather than the primary control.
            return 0.0, "board_mismatch_mandatory"

        # CBSE and state boards overlap heavily below Class 9.
        return 0.55, "board_mismatch_tolerable"

    def _exam_fit(self, required_exam: str, tutor: TutorCandidate) -> tuple[float, str]:
        needed = academics.EXAM_SUBJECTS.get(required_exam)
        if not needed:
            # Exam with no defined subject profile (e.g. Board Exam, SAT). Fall
            # back to whether the tutor teaches senior classes at all.
            senior = any(
                (n := academics.class_number(c)) is not None and n >= 9
                for c in tutor.capabilities.classes
            )
            return (0.8, "exam_senior_capable") if senior else (0.4, "exam_unverified")

        taught = {s.lower() for s in tutor.capabilities.subjects}
        covered = sum(1 for subject in needed if subject.lower() in taught)
        if covered == 0:
            return 0.0, f"exam_no_subject_overlap:{required_exam}"
        return covered / len(needed), f"exam_subject_overlap:{covered}/{len(needed)}"

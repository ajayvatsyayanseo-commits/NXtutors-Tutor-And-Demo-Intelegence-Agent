"""Deterministic policy selection.

Which policy ranks a requirement is decided by explicit rules on structured
fields — never by a model. Letting an LLM pick the policy would let it pick the
weights that rank the results it is then asked to explain.

Precedence is ordered most-specific first and documented per rule, because two
rules can legitimately both apply (an urgent JEE request is both urgent and
competitive) and the tie has to be predictable.
"""

from __future__ import annotations

from tutor_match_meta.contracts.common import TuitionMode, Urgency
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.domain import academics

COMPETITIVE_EXAM_POLICY = "competitive_exam.v1"
BOARD_EXAM_POLICY = "board_exam_prep.v1"
URGENT_POLICY = "urgent_tuition.v1"
HOME_POLICY = "home_tuition.v1"
ONLINE_POLICY = "online_tuition.v1"
HYBRID_POLICY = "hybrid_tuition.v1"
REGULAR_POLICY = "regular_school_support.v1"

#: Exams that make a request specialist rather than school-support.
_COMPETITIVE = frozenset(
    {
        academics.JEE,
        academics.NEET,
        academics.NTSE,
        academics.CUET,
        academics.OLYMPIAD,
        academics.SAT,
    }
)

_BY_MODE: dict[TuitionMode, str] = {
    TuitionMode.HOME: HOME_POLICY,
    TuitionMode.ONLINE: ONLINE_POLICY,
    TuitionMode.HYBRID: HYBRID_POLICY,
}


def select_policy(requirement: MatchRequirementV1) -> tuple[str, str]:
    """Return `(policy_name, reason_code)` for a requirement.

    Precedence, highest first:

    1. **Competitive exam.** A JEE/NEET requirement is a specialist search
       whatever the mode or urgency — a nearby generalist is not a substitute.
    2. **Board exam year.** Class 10/12, or an explicit board-exam goal.
    3. **Urgency.** Only reaches here for non-exam requests: an urgent board-exam
       parent still needs the right board tutor more than they need speed.
    4. **Mode.** The everyday distinction.
    5. **Regular school support.** The default.
    """
    exam = academics.normalize_exam(requirement.value_of("exam"))
    if exam in _COMPETITIVE:
        return COMPETITIVE_EXAM_POLICY, f"competitive_exam:{exam}"

    class_label = requirement.value_of("student_class")
    grade = academics.class_number(class_label)
    if exam == academics.BOARD_EXAM or grade in (10, 12):
        return BOARD_EXAM_POLICY, f"board_exam_year:{class_label or exam}"

    if requirement.urgency is Urgency.URGENT:
        return URGENT_POLICY, "urgency:urgent"

    mode = requirement.value_of("mode")
    if isinstance(mode, TuitionMode):
        return _BY_MODE[mode], f"mode:{mode}"

    return REGULAR_POLICY, "default"

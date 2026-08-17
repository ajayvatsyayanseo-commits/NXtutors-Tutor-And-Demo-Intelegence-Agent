"""Stage C — deterministic hard constraints.

Everything here is a boolean rule over structured data. **No LLM output can add,
remove, weaken or override a rule in this module.** The orchestrator applies
these before any scoring runs, and a rejection is recorded with its reason so a
"no match" answer is explainable.

Two design rules keep the filters from being over-eager, which is the failure
mode that quietly empties a candidate pool:

* **Absent tutor data is never a rejection.** A blank `board` column means
  unknown, not incompatible. Filters reject on *contradiction*, not on silence.
* **A requirement field can only filter when it is trustworthy.** A low-confidence
  LLM guess of "ICSE" is a scoring signal, not a deletion (see
  `MatchRequirementV1.usable_for_hard_filter`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tutor_match_meta.contracts.common import RECOMMENDABLE_FRESHNESS, TuitionMode
from tutor_match_meta.contracts.scoring import HardFilterRejection
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.domain import academics, localities, subjects
from tutor_match_meta.matching.base import EvaluationContext
from tutor_match_meta.matching.proximity import ProximityEvaluator

#: `(rule_name, predicate)`. The predicate returns a rejection detail string when
#: the tutor must be removed, or None to keep them.
FilterRule = Callable[[TutorCandidate, EvaluationContext], str | None]


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    survivors: tuple[TutorCandidate, ...]
    rejections: tuple[HardFilterRejection, ...]

    @property
    def empty(self) -> bool:
        return not self.survivors


# --------------------------------------------------------------------- rules
def active_tutor(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """The tutor's public profile must actually load.

    The website route requires `join_as='teacher' AND status='t'`; a projection
    row that is too stale may describe an account deactivated since the last
    sync, and linking to it is a hard 404 in the parent's WhatsApp.
    """
    _ = context
    if tutor.freshness not in RECOMMENDABLE_FRESHNESS:
        return f"projection freshness={tutor.freshness}"
    return None


def subject_supported(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """A Hindi tutor must never answer a Maths request."""
    requirement = context.requirement
    if not requirement.usable_for_hard_filter("subject"):
        return None
    required = requirement.value_of("subject")
    if not required or not tutor.capabilities.subjects:
        return None  # Unknown capability is not a contradiction.
    if subjects.matches_any(required, list(tutor.capabilities.subjects)):
        return None
    return f"does not teach {required}"


def class_supported(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """Grade must be inside the tutor's declared range, when they declared one."""
    requirement = context.requirement
    if not requirement.usable_for_hard_filter("student_class"):
        return None
    required = requirement.value_of("student_class")
    if not required or not tutor.capabilities.classes:
        return None
    if academics.teaches_class(required, list(tutor.capabilities.classes)):
        return None
    return f"does not teach {required}"


def board_supported(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """Only filters when the board genuinely matters at this grade.

    IB/IGCSE/Cambridge/ISC always diverge; Indian boards only diverge enough to
    be disqualifying from Class 9 (see `academics.board_is_mandatory`). Filtering
    a Class 4 parent on CBSE-vs-state would delete good tutors for nothing.
    """
    requirement = context.requirement
    if not requirement.usable_for_hard_filter("board"):
        return None
    required = academics.normalize_board(requirement.value_of("board"))
    if not required or not tutor.capabilities.boards:
        return None
    grade = academics.class_number(requirement.value_of("student_class"))
    if not academics.board_is_mandatory(required, grade):
        return None
    taught = {academics.normalize_board(b) for b in tutor.capabilities.boards}
    return None if required in taught else f"does not teach {required}"


def mode_supported(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """An online-only tutor cannot do home tuition, and vice versa."""
    requirement = context.requirement
    if not requirement.usable_for_hard_filter("mode"):
        return None
    required = requirement.value_of("mode")
    if not isinstance(required, TuitionMode):
        return None
    return None if tutor.supports_mode(required) else f"does not offer {required.value}"


def city_reachable(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """Home tuition in a different city is not feasible.

    Only applies to home mode, and only when both cities are known. Online and
    hybrid requests are exempt.
    """
    requirement = context.requirement
    if requirement.value_of("mode") is not TuitionMode.HOME:
        return None
    parent_city = requirement.location.city
    if not parent_city or not tutor.city:
        return None
    if localities.same_city(parent_city, tutor.city):
        return None
    return f"tutor city {tutor.city} != {parent_city}"


def within_travel_radius(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """Distance beyond a generous multiple of the assumed radius.

    The radius itself is a policy default, not a tutor-declared fact, so this
    filter only fires on clear failures (1.5× the radius) and never when
    coordinates are unavailable.
    """
    if context.requirement.value_of("mode") is not TuitionMode.HOME:
        return None
    verdict = ProximityEvaluator().within_radius(tutor, context)
    if verdict is None or verdict:
        return None
    return "outside feasible travel radius"


def fee_within_negotiable_range(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """The tutor's floor is beyond what any approved strategy can close.

    Only fires when both numbers exist. The tutor's fee unit is unknown
    (assumptions A3), so this uses the policy's generous `max_over_budget_ratio`
    rather than a strict comparison.
    """
    budget, fee = context.requirement.budget, tutor.fee
    if not budget or not fee or budget.maximum is None or fee.minimum is None:
        return None
    if budget.maximum <= 0:
        return None
    ratio = fee.minimum / budget.maximum
    limit = context.policy.negotiation.max_over_budget_ratio
    if ratio <= limit:
        return None
    return f"minimum fee {ratio:.1f}x the stated budget ceiling"


def gender_if_requested(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """Applies only when the parent explicitly asked (docs/assumptions.md A11).

    An unrequested gender filter would be discriminatory; a requested one is a
    legitimate safety/comfort preference that the website already honours.
    """
    requirement = context.requirement
    if not requirement.usable_for_hard_filter("tutor_gender_preference"):
        return None
    wanted = (requirement.value_of("tutor_gender_preference") or "").strip().lower()
    if wanted not in {"male", "female"}:
        return None
    if not tutor.gender:
        return None  # Unknown is not a contradiction.
    return None if tutor.gender.strip().lower() == wanted else f"gender != {wanted}"


def explicit_tutor_request(tutor: TutorCandidate, context: EvaluationContext) -> str | None:
    """When a parent named one tutor, everyone else is out of scope."""
    wanted = context.requirement.explicit_tutor_ref
    if not wanted:
        return None
    return None if tutor.public_ref == wanted else "parent requested a specific tutor"


#: Applied in order. Cheapest and most-discriminating first so the expensive
#: geometric check runs on the smallest possible set.
RULES: tuple[tuple[str, FilterRule], ...] = (
    ("ACTIVE_TUTOR", active_tutor),
    ("EXPLICIT_TUTOR_REQUEST", explicit_tutor_request),
    ("SUBJECT_SUPPORTED", subject_supported),
    ("CLASS_SUPPORTED", class_supported),
    ("BOARD_SUPPORTED", board_supported),
    ("MODE_SUPPORTED", mode_supported),
    ("GENDER_IF_REQUESTED", gender_if_requested),
    ("CITY_REACHABLE", city_reachable),
    ("FEE_NEGOTIABLE", fee_within_negotiable_range),
    ("WITHIN_TRAVEL_RADIUS", within_travel_radius),
)


def apply(
    candidates: Iterable[TutorCandidate],
    context: EvaluationContext,
    *,
    rules: tuple[tuple[str, FilterRule], ...] = RULES,
) -> FilterOutcome:
    """Run every rule, keeping a reason for each rejection.

    Stops at the first failing rule per tutor: the first reason is the most
    important one, and evaluating the rest would only add noise to the audit log.
    """
    survivors: list[TutorCandidate] = []
    rejections: list[HardFilterRejection] = []

    for tutor in candidates:
        rejected = False
        for rule_name, rule in rules:
            detail = rule(tutor, context)
            if detail is not None:
                rejections.append(
                    HardFilterRejection(
                        tutor_id=tutor.tutor_id, rule=rule_name, detail=detail[:240]
                    )
                )
                rejected = True
                break
        if not rejected:
            survivors.append(tutor)

    return FilterOutcome(survivors=tuple(survivors), rejections=tuple(rejections))


def rejection_summary(rejections: Iterable[HardFilterRejection]) -> dict[str, int]:
    """Rejections per rule. Drives the 'why no match' diagnosis and metrics."""
    counts: dict[str, int] = {}
    for rejection in rejections:
        counts[rejection.rule] = counts.get(rejection.rule, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

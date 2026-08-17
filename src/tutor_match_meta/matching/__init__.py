"""The eight bounded matching skills, plus deterministic hard filters.

Each evaluator is pure, total and independently testable — see
`matching/base.py` for the contract they all satisfy. The registry below is the
single place the orchestrator learns which skills exist.
"""

from tutor_match_meta.contracts.common import Dimension
from tutor_match_meta.matching.academic import AcademicCompatibilityEvaluator
from tutor_match_meta.matching.availability import AvailabilityEvaluator
from tutor_match_meta.matching.base import EvaluationContext, SkillEvaluator
from tutor_match_meta.matching.negotiation import NegotiationEvaluator
from tutor_match_meta.matching.performance import PerformanceEvaluator
from tutor_match_meta.matching.personality import PersonalityCompatibilityEvaluator
from tutor_match_meta.matching.proximity import ProximityEvaluator
from tutor_match_meta.matching.replacement_risk import ReplacementRiskEvaluator
from tutor_match_meta.matching.subject_expertise import SubjectExpertiseEvaluator


def default_evaluators() -> dict[Dimension, SkillEvaluator]:
    """All eight skills, keyed by dimension.

    Constructed fresh per call rather than as a module-level singleton: the
    evaluators are stateless, and a shared instance across warm Lambda
    invocations is an easy place for someone to later add a cache that leaks
    one conversation's data into the next.
    """
    evaluators: tuple[SkillEvaluator, ...] = (
        AcademicCompatibilityEvaluator(),
        SubjectExpertiseEvaluator(),
        AvailabilityEvaluator(),
        ProximityEvaluator(),
        PerformanceEvaluator(),
        PersonalityCompatibilityEvaluator(),
        NegotiationEvaluator(),
        ReplacementRiskEvaluator(),
    )
    return {evaluator.dimension: evaluator for evaluator in evaluators}


__all__ = [
    "AcademicCompatibilityEvaluator",
    "AvailabilityEvaluator",
    "EvaluationContext",
    "NegotiationEvaluator",
    "PerformanceEvaluator",
    "PersonalityCompatibilityEvaluator",
    "ProximityEvaluator",
    "ReplacementRiskEvaluator",
    "SkillEvaluator",
    "SubjectExpertiseEvaluator",
    "default_evaluators",
]

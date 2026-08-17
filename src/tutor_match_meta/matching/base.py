"""The evaluator contract.

Every skill module implements `SkillEvaluator`. The constraints are deliberate
and enforced by `tests/unit/matching/test_evaluator_contract.py`:

* **Pure.** No I/O, no clock reads, no database, no network, no LLM. Everything
  an evaluator needs arrives in `EvaluationContext`.
* **Total.** It must return a `SkillScore` for every candidate, including ones it
  knows nothing about — `DataQuality.MISSING` is an answer, an exception is not.
* **No side effects.** It cannot send a message, write a row, or mutate its input.
* **Independently testable.** Constructing one requires only a policy.

That is what makes eight skills composable without eight agents talking to
each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from tutor_match_meta.contracts.common import DataQuality, Dimension, Evidence
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import SkillScore
from tutor_match_meta.contracts.tutor import TutorCandidate
from tutor_match_meta.scoring.policy import ScoringPolicy


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Everything an evaluator is allowed to see.

    `now` is passed in rather than read from the clock so that scoring is
    reproducible: replaying a decision six months later must produce the same
    recency weighting it produced at the time.
    """

    requirement: MatchRequirementV1
    policy: ScoringPolicy
    now: datetime
    #: Optional curriculum/syllabus context retrieved from RAG. Always DATA,
    #: never instructions — sanitised before it gets here.
    knowledge: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class SkillEvaluator(Protocol):
    """One bounded matching skill."""

    dimension: Dimension

    def evaluate(self, tutor: TutorCandidate, context: EvaluationContext) -> SkillScore:
        """Score one tutor. Must never raise for a well-formed candidate."""
        ...


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def scaled(value: float, *, best: float, worst: float) -> float:
    """Linear 0-1 scale where `best` maps to 1.0 and `worst` to 0.0.

    Handles an inverted scale (worst > best, e.g. distance in km) so callers do
    not each reinvent the arithmetic and get a sign wrong.
    """
    if best == worst:
        return 1.0
    return clamp((value - worst) / (best - worst))


def evidence(source: str, field_name: str, value: object, when: datetime | None = None) -> Evidence:
    return Evidence(source=source, field=field_name, value=str(value)[:240], observed_at=when)


def cap_confidence(confidence: float, quality: DataQuality) -> float:
    """Enforce the contract's confidence ceiling for degraded data.

    `SkillScore` rejects high confidence on non-OK data quality; evaluators call
    this instead of each remembering the exact ceiling.
    """
    ceiling = 1.0 if quality is DataQuality.OK else 0.85
    return round(clamp(min(confidence, ceiling)), 3)


def build(
    dimension: Dimension,
    *,
    score: float,
    confidence: float,
    quality: DataQuality,
    evidences: tuple[Evidence, ...] = (),
    flags: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
) -> SkillScore:
    """Construct a `SkillScore` with the invariants already satisfied."""
    if quality is DataQuality.MISSING:
        evidences = ()
    return SkillScore(
        dimension=dimension,
        score=round(clamp(score), 4),
        confidence=cap_confidence(confidence, quality),
        data_quality=quality,
        evidence=evidences,
        flags=flags,
        reason_codes=reasons,
    )

"""Vocabulary shared by every contract in the service.

These enums are the reason the matcher can be honest: `DataQuality` forces every
evaluator to declare how good its inputs were, and `Provenance` forces every
extracted requirement field to declare where it came from. Nothing in the system
is allowed to hold a bare value with no story attached.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

SCHEMA_VERSION = "1.0"


class TuitionMode(StrEnum):
    HOME = "home"
    ONLINE = "online"
    HYBRID = "hybrid"


class Urgency(StrEnum):
    STANDARD = "standard"
    SOON = "soon"
    URGENT = "urgent"


class Dimension(StrEnum):
    """The eight bounded skills, in the order they are reported."""

    ACADEMIC = "academic"
    SUBJECT_EXPERTISE = "subject_expertise"
    AVAILABILITY = "availability"
    PROXIMITY = "proximity"
    PERFORMANCE = "performance"
    PERSONALITY = "personality"
    NEGOTIATION = "negotiation"
    REPLACEMENT_RISK = "replacement_risk"


#: Dimensions whose reasoning is internal-only and must never reach a parent.
INTERNAL_ONLY_DIMENSIONS: frozenset[Dimension] = frozenset({Dimension.REPLACEMENT_RISK})


class DataQuality(StrEnum):
    """How much the evaluator's inputs can be trusted.

    OK           full evidence available
    PARTIAL      some evidence, some gaps
    INSUFFICIENT evidence exists but is below the policy's minimum sample
    MISSING      no evidence at all for this dimension
    STALE        evidence exists but is older than the freshness policy
    """

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    MISSING = "missing"
    STALE = "stale"


#: Data qualities that forbid the explanation layer from citing the dimension.
UNQUOTABLE_QUALITY: frozenset[DataQuality] = frozenset(
    {DataQuality.MISSING, DataQuality.INSUFFICIENT, DataQuality.STALE}
)


class Provenance(StrEnum):
    """Where a requirement field's value came from. Ordered by trust, ascending."""

    POLICY_DEFAULT = "policy_default"
    LLM = "llm"
    MEMORY = "memory"
    LEAD_INTAKE = "lead_intake"
    DETERMINISTIC = "deterministic"
    USER_CONFIRMED = "user_confirmed"


#: Higher wins when two sources disagree about the same field.
PROVENANCE_RANK: dict[Provenance, int] = {
    Provenance.POLICY_DEFAULT: 0,
    Provenance.LLM: 1,
    Provenance.MEMORY: 2,
    Provenance.LEAD_INTAKE: 3,
    Provenance.DETERMINISTIC: 4,
    Provenance.USER_CONFIRMED: 5,
}


class Freshness(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


#: Freshness values a tutor may be recommended under (docs/assumptions.md A16).
RECOMMENDABLE_FRESHNESS: frozenset[Freshness] = frozenset({Freshness.FRESH, Freshness.AGING})


class Tracked(BaseModel, Generic[T]):
    """A value that carries its own confidence and origin.

    Requirement fields are `Tracked` rather than bare values so that
    "the parent told us Class 10" and "a model guessed Class 10" can never be
    confused at the point where a hard filter is applied.
    """

    model_config = ConfigDict(frozen=True)

    value: T
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    provenance: Provenance = Provenance.DETERMINISTIC
    observed_at: datetime | None = None

    def beats(self, other: Tracked[T] | None) -> bool:
        """True when this value should replace `other`.

        Trust rank dominates; confidence breaks ties within the same rank; and
        when both of those tie, the *later* observation wins.

        A confident guess never overrides something the parent actually said.
        But two things the parent said are not a tie to resolve in favour of the
        older one — that made self-correction impossible ("class 9 — sorry,
        class 10" stayed Class 9, because both parses were DETERMINISTIC at the
        same confidence). The most recent statement is the current requirement.
        """
        if other is None:
            return True
        mine, theirs = PROVENANCE_RANK[self.provenance], PROVENANCE_RANK[other.provenance]
        if mine != theirs:
            return mine > theirs
        if self.confidence != other.confidence:
            return self.confidence > other.confidence
        if self.observed_at is None or other.observed_at is None:
            return False
        return self.observed_at > other.observed_at


class Evidence(BaseModel):
    """A single grounded fact backing a score. No evidence, no claim."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(max_length=80, description="e.g. website.teacher_courses")
    field: str = Field(max_length=64)
    value: str = Field(max_length=240)
    observed_at: datetime | None = None

    def render(self) -> str:
        return f"{self.source}.{self.field}={self.value}"


class MissingField(BaseModel):
    """A requirement field the matcher needs but does not have."""

    model_config = ConfigDict(frozen=True)

    field: str
    blocking: bool = Field(description="True when no useful match is possible without it")
    ask_priority: int = Field(ge=0, le=100, description="Lower is asked first")

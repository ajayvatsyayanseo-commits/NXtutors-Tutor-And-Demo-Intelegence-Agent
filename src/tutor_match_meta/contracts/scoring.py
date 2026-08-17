"""Scoring and decision contracts.

`SkillScore` is the single output shape of all eight evaluators. It cannot be
constructed without declaring data quality, and a score claiming `DataQuality.OK`
with no evidence is rejected at validation time — the type system enforces the
"no score without provenance" rule rather than relying on reviewers to notice.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import (
    UNQUOTABLE_QUALITY,
    DataQuality,
    Dimension,
    Evidence,
    Freshness,
)


class SkillScore(BaseModel):
    """One dimension's verdict on one tutor."""

    model_config = ConfigDict(frozen=True)

    dimension: Dimension
    #: 0.0 = worst plausible fit, 1.0 = ideal. Never a raw star rating.
    score: float = Field(ge=0.0, le=1.0)
    #: How much the orchestrator should trust `score`. Low confidence shrinks the
    #: dimension's effective weight rather than the score itself, so a missing
    #: signal cannot masquerade as a bad one.
    confidence: float = Field(ge=0.0, le=1.0)
    data_quality: DataQuality
    evidence: tuple[Evidence, ...] = ()
    flags: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _quality_matches_evidence(self) -> Self:
        if self.data_quality is DataQuality.OK and not self.evidence:
            raise ValueError(f"{self.dimension}: data_quality=ok requires at least one evidence")
        if self.data_quality is DataQuality.MISSING and self.evidence:
            raise ValueError(f"{self.dimension}: data_quality=missing cannot carry evidence")
        if self.data_quality is not DataQuality.OK and self.confidence > 0.85:
            raise ValueError(
                f"{self.dimension}: confidence {self.confidence} too high for "
                f"data_quality={self.data_quality}"
            )
        return self

    @property
    def quotable(self) -> bool:
        """True when the explanation layer may cite this dimension to a parent."""
        return self.data_quality not in UNQUOTABLE_QUALITY and bool(self.evidence)


def missing_score(
    dimension: Dimension, *, reason: str, neutral: float, confidence: float = 0.1
) -> SkillScore:
    """The honest answer when a dimension has nothing to work with.

    Returns the policy's neutral prior rather than 0.0. Scoring a tutor badly for
    data the *website* never collected would systematically bury tutors whose
    profiles are merely incomplete.
    """
    return SkillScore(
        dimension=dimension,
        score=neutral,
        confidence=confidence,
        data_quality=DataQuality.MISSING,
        reason_codes=(reason,),
    )


class HardFilterRejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    tutor_id: str
    rule: str = Field(max_length=64)
    detail: str = Field(max_length=240)


class ScoredCandidate(BaseModel):
    """A candidate that survived hard filters, with its full skill vector."""

    model_config = ConfigDict(frozen=True)

    tutor_id: str
    public_ref: str
    #: Pseudonym handed to the LLM in place of any real identifier.
    pseudonym: str = Field(max_length=32)
    scores: dict[Dimension, SkillScore]
    final_score: float = Field(ge=0.0, le=1.0)
    #: Sum of the weights that actually contributed. Low coverage means the final
    #: score rests on few dimensions and the shortlist should say less about it.
    weight_coverage: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    freshness: Freshness
    flags: tuple[str, ...] = ()

    def quotable_scores(self) -> list[SkillScore]:
        from tutor_match_meta.contracts.common import INTERNAL_ONLY_DIMENSIONS

        return [
            score
            for dimension, score in self.scores.items()
            if score.quotable and dimension not in INTERNAL_ONLY_DIMENSIONS
        ]


class ShortlistEntry(BaseModel):
    """One tutor as presented to the parent. Every field here is grounded."""

    model_config = ConfigDict(frozen=True)

    rank: int = Field(ge=1)
    tutor_id: str
    name: str
    profile_url: str
    reasons: tuple[str, ...] = Field(description="Plain-language, evidence-backed")
    mode_label: str | None = None
    locality_label: str | None = None
    availability_label: str | None = None
    fee_label: str | None = None
    #: Internal only. Excluded from every parent-facing rendering.
    internal_notes: tuple[str, ...] = ()


class MatchDecisionV1(BaseModel):
    """The full, auditable record of one matching run.

    Persisted verbatim. This is what makes a shortlist explainable six months
    later: the policy version, the candidate pool, why each rejection happened,
    and the exact skill vector behind each rank.
    """

    model_config = ConfigDict(frozen=True)

    match_session_id: str
    conversation_id: str
    trace_id: str
    policy_id: str
    policy_version: str
    #: sha256 of the resolved policy document; detects an unversioned edit.
    policy_checksum: str = Field(max_length=64)

    candidate_ids: tuple[str, ...]
    rejections: tuple[HardFilterRejection, ...] = ()
    scored: tuple[ScoredCandidate, ...] = ()
    shortlist: tuple[ShortlistEntry, ...] = ()

    no_match_reason: str | None = None
    requires_human_review: bool = False
    degraded_sources: tuple[str, ...] = Field(
        default=(), description="Dependencies that were down during this run"
    )
    oldest_source_data_at: datetime | None = None
    generated_at: datetime

    @property
    def matched(self) -> bool:
        return bool(self.shortlist)

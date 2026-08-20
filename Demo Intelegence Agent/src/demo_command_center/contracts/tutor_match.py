"""The Demo ↔ Tutor Intelligence contract. Demo-owned, versioned, Demo's shape.

Deliberately **not** an import of `tutor_match_meta.contracts`. Two reasons, and
both are load-bearing:

* Demo must deploy without Tutor Intelligence on the path. The adapter in
  `integrations/tutor_intelligence/` translates; the domain never sees a Tutor
  type. That is what lets the whole test suite and `make demo` run with
  `tutor_match_meta` uninstalled.
* Tutor Intelligence is a protected component. Importing its models into Demo's
  domain would make a refactor over there a breaking change over here, which is
  exactly the coupling the read-only rule exists to prevent.

Field names mirror Tutor's vocabulary (`data_quality`, `evidence`, `freshness`)
so the translation is obvious and the contract test can assert it field by
field.

**Nothing in a result is trusted because it is well-formed.** `TutorMatchResultV1`
is validated for staleness and for evidence *before* any candidate is presented —
see `validate_for_presentation`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import (
    SCHEMA_VERSION,
    DemoMode,
    RegionRef,
    StudentRef,
    TutorRef,
)

#: A result older than this may not be presented. A shortlist is a snapshot of
#: availability and fees; both move, and a parent booking against a two-hour-old
#: snapshot is how a tutor gets double-booked.
MAX_RESULT_AGE = timedelta(minutes=15)

#: How many options a parent is shown. Two or three — more is a worse decision,
#: not a better one, and every extra option is another profile we must verify.
MIN_OPTIONS = 2
MAX_OPTIONS = 3


class DataQuality(StrEnum):
    """Mirrors `tutor_match_meta.contracts.common.DataQuality` exactly."""

    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    MISSING = "missing"
    STALE = "stale"


#: Qualities that forbid quoting the dimension to a parent. Same set as Tutor's
#: `UNQUOTABLE_QUALITY`, restated here so Demo's guard does not depend on it.
UNQUOTABLE: frozenset[DataQuality] = frozenset(
    {DataQuality.MISSING, DataQuality.INSUFFICIENT, DataQuality.STALE}
)


class Freshness(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


#: Freshness values a candidate may be booked against.
BOOKABLE_FRESHNESS: frozenset[Freshness] = frozenset({Freshness.FRESH, Freshness.AGING})


class ScoreEvidence(BaseModel):
    """One grounded fact behind a dimension's score. No evidence, no claim."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(max_length=80)
    field_name: str = Field(max_length=64)
    value: str = Field(max_length=240)
    observed_at: datetime | None = None


class DimensionScore(BaseModel):
    """One of Tutor Intelligence's eight dimensions, as Demo receives it."""

    model_config = ConfigDict(frozen=True)

    dimension: str = Field(max_length=32)
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    data_quality: DataQuality
    evidence: tuple[ScoreEvidence, ...] = ()

    @property
    def quotable(self) -> bool:
        """Whether this dimension may be cited in a message to a parent."""
        return self.data_quality not in UNQUOTABLE and bool(self.evidence)


class TutorCandidateV1(BaseModel):
    """One ranked tutor. Every presentable field is grounded or absent.

    The optional-everything shape is intentional: Tutor Intelligence omits a
    label it cannot substantiate, and Demo must render the omission rather than
    fill it. A `fee_label` of `None` means "we do not know", and the message
    builder says nothing about fees — it does not say "fee on request", which
    is a claim we have not verified either.
    """

    model_config = ConfigDict(frozen=True)

    rank: int = Field(ge=1, le=MAX_OPTIONS)
    tutor_ref: TutorRef
    #: Display name as the website holds it. Never invented, never abbreviated.
    name: str = Field(min_length=1, max_length=160)
    profile_url: str = Field(min_length=1, max_length=512)
    #: Plain-language, evidence-backed reasons produced by Tutor Intelligence.
    reasons: tuple[str, ...] = ()

    mode_label: str | None = Field(default=None, max_length=64)
    locality_label: str | None = Field(default=None, max_length=120)
    availability_label: str | None = Field(default=None, max_length=160)
    fee_label: str | None = Field(default=None, max_length=64)

    scores: tuple[DimensionScore, ...] = ()
    final_score: float = Field(ge=0.0, le=1.0)
    #: Share of the scoring weight that actually had data behind it.
    weight_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    freshness: Freshness = Freshness.UNKNOWN

    @property
    def bookable(self) -> bool:
        return self.freshness in BOOKABLE_FRESHNESS

    def quotable_reasons(self) -> tuple[str, ...]:
        """Reasons we may show, gated on at least one quotable dimension.

        A candidate whose every dimension is `MISSING` can still be *offered* —
        it may simply be a new tutor — but nothing may be *claimed* about them.
        """
        return self.reasons if any(score.quotable for score in self.scores) else ()


class TutorMatchRequestV1(BaseModel):
    """What Demo asks Tutor Intelligence for."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    #: Preserved end to end. Tutor Intelligence stamps them onto its decision.
    trace_id: str = Field(max_length=64)
    correlation_id: str = Field(max_length=128)
    causation_id: str | None = Field(default=None, max_length=128)
    conversation_ref: str = Field(max_length=128)
    student_ref: StudentRef | None = None

    subject: str = Field(min_length=1, max_length=64)
    student_class: str = Field(min_length=1, max_length=16)
    board: str = Field(min_length=1, max_length=32)
    mode: DemoMode
    region: RegionRef | None = None
    locality: str | None = Field(default=None, max_length=120)
    timezone: str = "Asia/Kolkata"
    learning_goal: str | None = Field(default=None, max_length=256)
    language: str | None = Field(default=None, max_length=32)
    #: Refs already rejected in this conversation. Sent so "show me others"
    #: returns others, rather than the same top three with a new session id.
    exclude_tutor_refs: tuple[TutorRef, ...] = ()
    limit: int = Field(default=MAX_OPTIONS, ge=1, le=10)

    #: Always true. Present as an explicit field, not an assumption, so the
    #: adapter can assert it and a future transport cannot quietly drop it:
    #: Demo owns the conversation and Tutor Intelligence must not send anything.
    return_only: bool = True

    @model_validator(mode="after")
    def _return_only_is_not_negotiable(self) -> Self:
        if not self.return_only:
            raise ValueError(
                "Demo Command Center owns the conversation; a tutor match that "
                "sends its own message would double-send to the parent"
            )
        return self


class MatchRejection(BaseModel):
    """Why a tutor was excluded. Diagnostics for operators, never for parents."""

    model_config = ConfigDict(frozen=True)

    tutor_ref: str = Field(max_length=128)
    rule: str = Field(max_length=64)
    detail: str = Field(default="", max_length=240)


class TutorMatchResultV1(BaseModel):
    """What Tutor Intelligence returns. Untrusted until validated."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    trace_id: str = Field(max_length=64)
    correlation_id: str = Field(max_length=128)
    conversation_ref: str = Field(max_length=128)
    #: Tutor Intelligence's own decision id. Stored so a shortlist stays
    #: explainable against the exact policy that produced it.
    match_session_id: str = Field(max_length=128)
    policy_ref: str = Field(default="", max_length=128)

    candidates: tuple[TutorCandidateV1, ...] = ()
    rejections: tuple[MatchRejection, ...] = ()
    no_match_reason: str | None = Field(default=None, max_length=120)
    requires_human_review: bool = False
    degraded_sources: tuple[str, ...] = ()
    generated_at: datetime

    #: Set by the adapter after it confirms Tutor Intelligence sent nothing.
    #: A result that cannot assert this is refused before presentation.
    sender_was_silent: bool = True

    @property
    def matched(self) -> bool:
        return bool(self.candidates)

    def age(self, *, now: datetime) -> timedelta:
        return now - self.generated_at

    def stale(self, *, now: datetime, max_age: timedelta = MAX_RESULT_AGE) -> bool:
        return self.age(now=now) > max_age

    def validate_for_presentation(self, *, now: datetime) -> tuple[str, ...]:
        """Every reason this result must not be shown. Empty means it may be.

        Returns all problems rather than the first, because an operator
        debugging "why did nobody get options" needs the whole list, and
        because each is a distinct metric.
        """
        problems: list[str] = []
        if not self.sender_was_silent:
            problems.append("tutor_agent_may_have_sent_a_message")
        if self.stale(now=now):
            problems.append(f"result_stale:{int(self.age(now=now).total_seconds())}s")
        if self.generated_at > now + timedelta(minutes=1):
            # A future timestamp means clock skew or a fabricated result.
            problems.append("result_generated_in_the_future")
        if self.requires_human_review:
            problems.append("tutor_agent_requested_human_review")
        if not self.candidates:
            problems.append(self.no_match_reason or "no_candidates")
        unbookable = [c.tutor_ref for c in self.candidates if not c.bookable]
        if unbookable and len(unbookable) == len(self.candidates):
            problems.append("all_candidates_stale")
        return tuple(problems)

    def presentable(self, *, now: datetime) -> tuple[TutorCandidateV1, ...]:
        """Bookable candidates, capped and re-ranked densely.

        Re-ranking matters: dropping rank 2 for staleness would otherwise
        present "option 1" and "option 3" to a parent, which reads as a bug.
        """
        kept = [c for c in self.candidates if c.bookable][:MAX_OPTIONS]
        return tuple(c.model_copy(update={"rank": index}) for index, c in enumerate(kept, start=1))

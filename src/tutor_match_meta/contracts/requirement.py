"""`MatchRequirementV1` — the structured form of what a parent asked for.

Every field is `Tracked`, so the matcher always knows whether a value was said by
the parent, inferred deterministically, recalled from memory, or guessed by a
model. Hard filters are only allowed to act on values whose provenance and
confidence clear the policy bar — that is what stops a model hallucinating
"ICSE" into a filter that silently deletes every valid tutor.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import (
    SCHEMA_VERSION,
    Provenance,
    Tracked,
    TuitionMode,
    Urgency,
)
from tutor_match_meta.contracts.schedule import WeeklySchedule

#: Confidence a field needs before it may be used as a *hard* filter.
#: Below this it still informs scoring, it just cannot delete candidates.
HARD_FILTER_MIN_CONFIDENCE = 0.75


class BudgetBand(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum: int | None = Field(default=None, ge=0, le=1_000_000)
    maximum: int | None = Field(default=None, ge=0, le=1_000_000)
    currency: str = "INR"
    #: The parent's own words, kept verbatim so we never re-render a unit they
    #: did not use. The website has no fee unit either (assumptions A3).
    raw: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _ordered(self) -> BudgetBand:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("budget minimum cannot exceed maximum")
        return self

    def __bool__(self) -> bool:
        return self.minimum is not None or self.maximum is not None


class LocationRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str | None = Field(default=None, max_length=80)
    locality: str | None = Field(default=None, max_length=120)
    pincode: str | None = Field(default=None, pattern=r"^\d{6}$")
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    max_travel_km: float | None = Field(default=None, gt=0, le=100)

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    def __bool__(self) -> bool:
        return any((self.city, self.locality, self.pincode, self.has_coordinates))


class MatchRequirementV1(BaseModel):
    """What we know about the requirement, and how well we know it."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION

    # ------------------------------------------------------------- identity
    conversation_id: str = Field(max_length=128)
    parent_ref: str | None = Field(default=None, max_length=128, description="pseudonymous")
    student_ref: str | None = Field(default=None, max_length=128)
    lead_id: str | None = Field(default=None, max_length=128)

    # ------------------------------------------------------------- academic
    subject: Tracked[str] | None = None
    additional_subjects: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    weak_topics: tuple[str, ...] = ()
    board: Tracked[str] | None = None
    student_class: Tracked[str] | None = None
    exam: Tracked[str] | None = None
    learning_goal: Tracked[str] | None = None

    # -------------------------------------------------------------- teaching
    preferred_teaching_style: Tracked[str] | None = None
    language_preference: Tracked[str] | None = None
    tutor_gender_preference: Tracked[str] | None = None
    explicit_tutor_ref: str | None = Field(default=None, max_length=128)

    # -------------------------------------------------------------- logistics
    mode: Tracked[TuitionMode] | None = None
    location: LocationRequirement = LocationRequirement()
    preferred_schedule: WeeklySchedule | None = None
    timezone: str = "Asia/Kolkata"
    sessions_per_week: int | None = Field(default=None, ge=1, le=14)
    session_minutes: int | None = Field(default=None, ge=15, le=300)
    start_by: date | None = None

    # --------------------------------------------------------------- money
    budget: BudgetBand = BudgetBand()

    # --------------------------------------------------------------- intent
    urgency: Urgency = Urgency.STANDARD
    demo_requested: bool = False

    # ------------------------------------------------------------ metadata
    captured_at: datetime | None = None
    turn_count: int = Field(default=0, ge=0)

    # ------------------------------------------------------------- helpers
    def usable_for_hard_filter(self, field_name: str) -> bool:
        """True when a field is trustworthy enough to delete candidates with.

        A low-confidence model guess must never silently empty the candidate
        pool — it downgrades to a scoring signal instead.
        """
        tracked = getattr(self, field_name, None)
        if not isinstance(tracked, Tracked):
            return False
        if tracked.provenance in (Provenance.USER_CONFIRMED, Provenance.DETERMINISTIC):
            return True
        return tracked.confidence >= HARD_FILTER_MIN_CONFIDENCE

    def value_of(self, field_name: str) -> Any:
        tracked = getattr(self, field_name, None)
        return tracked.value if isinstance(tracked, Tracked) else None

    @property
    def all_subjects(self) -> tuple[str, ...]:
        primary = self.value_of("subject")
        subjects = ([primary] if primary else []) + list(self.additional_subjects)
        seen: dict[str, None] = {}
        for subject in subjects:
            seen.setdefault(subject, None)
        return tuple(seen)

    def merged_with(self, other: MatchRequirementV1) -> MatchRequirementV1:
        """Fold a newly-extracted requirement into this one.

        Per-field trust wins (see `Tracked.beats`), so a later low-confidence
        model guess cannot overwrite something the parent stated earlier. This is
        what makes incremental collection across many WhatsApp turns safe.
        """
        merged: dict[str, Any] = self.model_dump()
        for name, field in self.model_fields.items():
            new_value = getattr(other, name)
            old_value = getattr(self, name)
            if isinstance(new_value, Tracked):
                if new_value.beats(old_value if isinstance(old_value, Tracked) else None):
                    merged[name] = new_value
                continue
            if name in _IDENTITY_FIELDS:
                merged[name] = new_value or old_value
                continue
            if isinstance(new_value, tuple):
                merged[name] = tuple(dict.fromkeys([*old_value, *new_value]))
                continue
            if name in _REPLACE_IF_TRUTHY and new_value:
                merged[name] = new_value
                continue
            _ = field
        # Sticky intent: a demo asked for on turn 2 is still wanted on turn 5,
        # and urgency only ever ratchets up within a conversation.
        merged["demo_requested"] = self.demo_requested or other.demo_requested
        merged["urgency"] = max(self.urgency, other.urgency, key=_URGENCY_RANK.__getitem__)
        merged["turn_count"] = max(self.turn_count, other.turn_count)
        return MatchRequirementV1.model_validate(merged)


_URGENCY_RANK: dict[Urgency, int] = {Urgency.STANDARD: 0, Urgency.SOON: 1, Urgency.URGENT: 2}


_IDENTITY_FIELDS = frozenset(
    {"conversation_id", "parent_ref", "student_ref", "lead_id", "explicit_tutor_ref"}
)
_REPLACE_IF_TRUTHY = frozenset(
    {
        "location",
        "preferred_schedule",
        "budget",
        "sessions_per_week",
        "session_minutes",
        "start_by",
        "captured_at",
        "timezone",
    }
)

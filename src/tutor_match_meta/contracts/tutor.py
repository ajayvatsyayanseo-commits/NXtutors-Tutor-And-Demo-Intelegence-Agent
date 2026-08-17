"""`TutorCandidate` — the only shape tutor data takes inside this service.

This mirrors the Laravel `PublicTutorFieldMapper` allowlist exactly (see
docs/current-state-audit.md §2.1). It is an allowlist by construction: the model
has no field for `email`, `phone`, `password`, `otp`, `dob`, KYC documents or the
tutor's private street address, so those columns cannot leak even if a future
projection query selects them by accident.

Two fields are deliberately absent from the website schema and owned here
instead: `availability` and `coordinates` (docs/assumptions.md A4, A5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tutor_match_meta.contracts.common import Freshness, TuitionMode
from tutor_match_meta.contracts.schedule import WeeklySchedule

#: Columns from `register` that must never appear in this service. Asserted by
#: tests/security/test_no_private_columns.py against the projection SQL.
FORBIDDEN_TUTOR_FIELDS: frozenset[str] = frozenset(
    {
        "email",
        "phone",
        "password",
        "c_password",
        "otp",
        "otp_status",
        "dob",
        "document_type",
        "document_number",
        "frount_image",
        "back_image",
        "address",
    }
)


class ReviewAggregate(BaseModel):
    """Aggregated `teacher_review` rows. Raw review text never travels."""

    model_config = ConfigDict(frozen=True)

    count: int = Field(default=0, ge=0)
    rating_avg: float | None = Field(default=None, ge=0, le=5)
    expertise_avg: float | None = Field(default=None, ge=0, le=5)
    patience_avg: float | None = Field(default=None, ge=0, le=5)
    reliability_avg: float | None = Field(default=None, ge=0, le=5)
    communication_avg: float | None = Field(default=None, ge=0, le=5)
    latest_review_at: datetime | None = None

    def __bool__(self) -> bool:
        return self.count > 0


class FeeBand(BaseModel):
    """Parsed from `register.budget`. Unit is unknown and never invented (A3)."""

    model_config = ConfigDict(frozen=True)

    minimum: int | None = Field(default=None, ge=0)
    maximum: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=64)
    #: True only when the source explicitly stated a per-hour rate. The website
    #: schema never does, so this is False for every website-sourced tutor.
    unit_known: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("fee minimum cannot exceed maximum")
        return self

    def __bool__(self) -> bool:
        return self.minimum is not None or self.maximum is not None


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    #: How the point was derived — 'pincode', 'locality', 'city'. Precision
    #: degrades down that list and the proximity evaluator reports it as such.
    granularity: str = Field(max_length=16)
    resolved_at: datetime | None = None


class TutorCapabilities(BaseModel):
    """Union of both live course schemas (docs/assumptions.md A14)."""

    model_config = ConfigDict(frozen=True)

    subjects: tuple[str, ...] = ()
    boards: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    modes: tuple[TuitionMode, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.subjects or self.boards or self.classes)


class TutorCandidate(BaseModel):
    """One active tutor, as this service is allowed to see them."""

    model_config = ConfigDict(frozen=True)

    # ------------------------------------------------------------- identity
    #: `register.user_id` (string, not the autoincrement id) — assumptions A1.
    tutor_id: str = Field(max_length=128)
    #: URL-safe base64 of `f"{tutor_id}-nxt"`; the site's public token.
    public_ref: str = Field(max_length=256)
    name: str = Field(max_length=160)
    gender: str | None = Field(default=None, max_length=16)
    avatar_url: str | None = Field(default=None, max_length=512)

    # ------------------------------------------------------------ location
    city: str | None = Field(default=None, max_length=80)
    #: Locality label only. The raw `register.address` is never copied here.
    locality: str | None = Field(default=None, max_length=120)
    district: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    pincode: str | None = Field(default=None, max_length=12)
    geo: GeoPoint | None = None
    travel_radius_km: float | None = Field(default=None, gt=0, le=100)

    # -------------------------------------------------------- capabilities
    capabilities: TutorCapabilities = TutorCapabilities()
    experience_years: int | None = Field(default=None, ge=0, le=70)
    education: str | None = Field(default=None, max_length=400)
    #: Narrative from profile/profile_desc/pro_desc. Treated as DATA, never as
    #: instructions — sanitised through `security.injection` before any LLM sees it.
    profile_summary: str | None = Field(default=None, max_length=1_200)

    # -------------------------------------------------------------- signals
    fee: FeeBand = FeeBand()
    reviews: ReviewAggregate = ReviewAggregate()
    #: TMM-owned. Empty means unknown, never "unavailable" (A4).
    availability: WeeklySchedule | None = None
    active_students: int | None = Field(default=None, ge=0, description="TMM-owned workload signal")

    # ------------------------------------------------------------ freshness
    source_updated_at: datetime | None = None
    synced_at: datetime | None = None
    freshness: Freshness = Freshness.UNKNOWN
    projection_version: int = Field(default=1, ge=1)

    @property
    def has_availability_data(self) -> bool:
        return self.availability is not None and bool(self.availability.windows)

    def supports_mode(self, mode: TuitionMode) -> bool:
        """Unknown modes are permissive: absence of data is not a rejection."""
        if not self.capabilities.modes:
            return True
        if mode is TuitionMode.HYBRID:
            return True
        return mode in self.capabilities.modes

"""The demo aggregate: the request, the booking, the attendees, the outcome.

`Demo` is the row every capability reads and almost none of them write — writes
go through the orchestrator so that a state change and its side effect share one
transaction boundary.

Two fields carry more weight than their size suggests:

* `calendar_event_id` — the *logical* event. A reschedule patches this event
  rather than creating a second one, which is what stops a parent accumulating
  four calendar invites for one demo.
* `outcome.evidence_source` — how we know what happened. A no-show recorded with
  `evidence_source="llm"` is refused by the state machine's guard, so the field
  is not documentation, it is an enforcement input.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from demo_command_center.contracts.common import (
    SCHEMA_VERSION,
    ContactRef,
    DemoMode,
    DemoOutcome,
    Language,
    Party,
    RegionRef,
    Requirement,
    StudentRef,
    TutorRef,
)
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.shared.clock import ensure_utc

#: After this much time past the scheduled end, an unrecorded outcome is chased.
OUTCOME_GRACE = timedelta(minutes=30)


class AttendanceSignal(StrEnum):
    """How attendance was established, ordered by trust.

    `LLM_INFERENCE` is present so it can be *recorded* and *refused*: the
    extraction step may well conclude someone did not turn up, and that belongs
    in the audit trail — it just may not move the state machine on its own.
    """

    CALENDAR_RESPONSE = "calendar_response"
    MEET_PARTICIPATION = "meet_participation"
    TUTOR_REPORT = "tutor_report"
    STUDENT_REPORT = "student_report"
    OPERATOR_REVIEW = "operator_review"
    LLM_INFERENCE = "llm_inference"
    NONE = "none"


#: Signals a no-show may be *automatically* recorded from. Deliberately excludes
#: every self-report and the model: a tutor marking a student absent has an
#: incentive, and the model has no idea.
AUTHORITATIVE_ABSENCE: frozenset[AttendanceSignal] = frozenset(
    {
        AttendanceSignal.CALENDAR_RESPONSE,
        AttendanceSignal.MEET_PARTICIPATION,
        AttendanceSignal.OPERATOR_REVIEW,
    }
)


class DemoRequest(BaseModel):
    """What was asked for, before any tutor or time exists."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    request_id: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    student_ref: StudentRef | None = None
    requirement: Requirement = Requirement()
    language: Language = Language.EN
    region: RegionRef | None = None
    created_at: datetime
    #: Set once Tutor Intelligence has been asked. Links a demo to the exact
    #: ranking run that produced its candidates.
    match_session_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _utc(self) -> Self:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        return self

    @property
    def ready_to_match(self) -> bool:
        return self.requirement.complete


class DemoAttendee(BaseModel):
    """One party on a demo. Contacts are refs, never raw identifiers."""

    model_config = ConfigDict(frozen=True)

    party: Party
    ref: str = Field(max_length=128)
    display_name: str = Field(default="", max_length=160)
    contacts: tuple[ContactRef, ...] = ()
    #: Whether they may be added to the calendar invite. Consent is resolved by
    #: the website gateway; absent consent means absent from the invite, not a
    #: default of "probably fine".
    invite_consent: bool = False

    def contact_for(self, channel: str) -> ContactRef | None:
        for contact in self.contacts:
            if contact.channel.value == channel and contact.contactable:
                return contact
        return None


class DemoOutcomeRecord(BaseModel):
    """What actually happened, and how we know."""

    model_config = ConfigDict(frozen=True)

    outcome: DemoOutcome = DemoOutcome.UNKNOWN
    student_attended: bool | None = None
    tutor_attended: bool | None = None
    evidence_source: AttendanceSignal = AttendanceSignal.NONE
    recorded_at: datetime | None = None
    recorded_by: str = Field(default="", max_length=64)
    notes: str = Field(default="", max_length=1_000)
    #: Minutes the demo actually ran, when a conference reports it.
    duration_minutes: int | None = Field(default=None, ge=0, le=600)

    @property
    def authoritative(self) -> bool:
        """Whether this record may drive an automatic state transition."""
        return self.evidence_source in AUTHORITATIVE_ABSENCE

    @property
    def is_no_show(self) -> bool:
        return self.student_attended is False or self.tutor_attended is False


class Demo(BaseModel):
    """A scheduled (or once-scheduled) demo class."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    demo_id: str = Field(max_length=64)
    conversation_ref: str = Field(max_length=128)
    request_id: str = Field(max_length=64)

    student_ref: StudentRef | None = None
    tutor_ref: TutorRef | None = None
    region: RegionRef | None = None
    mode: DemoMode = DemoMode.ONLINE
    language: Language = Language.EN

    slot: TimeSlot | None = None
    #: The logical calendar event. Patched on reschedule, never duplicated.
    calendar_event_id: str | None = Field(default=None, max_length=256)
    #: Only ever a Google-issued URL, validated against `MEET_POLICY`.
    meet_url: str | None = Field(default=None, max_length=512)
    #: For home demos. Deliberately coarse — a full street address is not
    #: needed to run the funnel and is a liability to hold.
    location_label: str | None = Field(default=None, max_length=200)

    attendees: tuple[DemoAttendee, ...] = ()
    outcome: DemoOutcomeRecord = DemoOutcomeRecord()

    created_at: datetime
    updated_at: datetime
    #: Increments on every reschedule. Reminder rows carry it, so a stale
    #: reminder for a moved demo is identifiable without a join.
    revision: int = Field(default=1, ge=1)
    cancelled_at: datetime | None = None
    cancellation_reason: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "updated_at", ensure_utc(self.updated_at))
        if self.mode is DemoMode.HOME and self.meet_url:
            # A Meet link on a home demo means a code path invented one.
            raise ValueError("an in-person demo must not carry a Meet URL")
        if self.meet_url and not self.calendar_event_id:
            raise ValueError("a Meet URL without a calendar event is not verifiable")
        return self

    @property
    def scheduled(self) -> bool:
        return self.slot is not None and self.calendar_event_id is not None

    @property
    def cancelled(self) -> bool:
        return self.cancelled_at is not None

    def attendee(self, party: Party) -> DemoAttendee | None:
        for person in self.attendees:
            if person.party is party:
                return person
        return None

    def outcome_due(self, *, now: datetime, grace: timedelta = OUTCOME_GRACE) -> bool:
        if self.slot is None or self.cancelled:
            return False
        return ensure_utc(now) >= self.slot.ends_at + grace

    def with_slot(self, slot: TimeSlot, *, now: datetime) -> Demo:
        """A reschedule. Bumps `revision` so old reminders are identifiable."""
        return self.model_copy(
            update={"slot": slot, "revision": self.revision + 1, "updated_at": ensure_utc(now)}
        )

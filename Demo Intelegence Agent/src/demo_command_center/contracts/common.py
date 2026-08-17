"""Shared vocabulary: opaque references, enums and the schema version.

The reference types are the privacy model in code form. This service works with
`StudentRef`/`TutorRef` — opaque handles the NXTutors website issues and can
resolve to a real contact at send time — and never with a phone number, email or
name it has stored itself. That is what makes "we do not duplicate website PII"
a structural property rather than a policy people have to remember.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Final = "1"

#: Opaque website-issued references. Constrained so an LLM-supplied value that
#: is actually a phone number or an injection payload fails validation.
_REF = Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]

StudentRef = _REF
TutorRef = _REF
PlanRef = _REF
SubscriptionRef = _REF
RegionRef = _REF


class DemoMode(StrEnum):
    ONLINE = "online"
    HOME = "home"


class Language(StrEnum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


class Party(StrEnum):
    """Who a message, reminder or no-show concerns."""

    STUDENT = "student"
    TUTOR = "tutor"
    BOTH = "both"


class Confidence(StrEnum):
    """How much weight a downstream decision may put on a value.

    Three levels rather than a float because the consumers are branches, not
    arithmetic: LOW means "do not act on this alone".
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Evidence(StrEnum):
    """How a derived fact was established. Load-bearing for objections.

    An `INFERRED` objection may be summarised for a human; it may never be
    quoted back to a parent as something they said.
    """

    EXPLICIT = "explicit"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class DemoOutcome(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    NOT_HELD = "not_held"
    UNKNOWN = "unknown"


class ContactChannel(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    CALENDAR_INVITE = "calendar_invite"


class ContactRef(BaseModel):
    """A resolvable contact. Never carries the underlying identifier.

    `masked` exists purely so an ops console and a human handoff summary can
    show a person enough to recognise the record without the record itself being
    a PII store.
    """

    model_config = ConfigDict(frozen=True)

    ref: _REF
    channel: ContactChannel
    #: `•••••3210`. Display only — never compared, never used for routing.
    masked: str = Field(default="", max_length=64)
    #: Consent and do-not-contact are resolved by the website, not guessed here.
    contactable: bool = True


class Requirement(BaseModel):
    """What a demo needs before it can be scheduled.

    Populated incrementally across turns. `missing()` drives what the assistant
    asks next, which is how it avoids asking for something already known.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    service: str | None = Field(default=None, max_length=64)
    board: str | None = Field(default=None, max_length=32)
    student_class: str | None = Field(default=None, max_length=16)
    subject: str | None = Field(default=None, max_length=64)
    mode: DemoMode | None = None
    region: RegionRef | None = None
    locality: str | None = Field(default=None, max_length=96)
    timezone: str = "Asia/Kolkata"
    availability_note: str | None = Field(default=None, max_length=256)
    special_requirements: str | None = Field(default=None, max_length=512)
    language: Language = Language.EN

    #: Fields required before matching may begin. `region` is required only for
    #: home mode, which `missing()` handles — asking an online student for their
    #: locality is the exact "question whose answer we do not need" the spec
    #: forbids. ClassVar, not Final: pydantic would otherwise make this a field
    #: and serialise the list into every stored requirement.
    REQUIRED: ClassVar[tuple[str, ...]] = (
        "service",
        "board",
        "student_class",
        "subject",
        "mode",
    )

    def missing(self) -> tuple[str, ...]:
        absent = [name for name in self.REQUIRED if getattr(self, name) is None]
        if self.mode is DemoMode.HOME and not (self.region or self.locality):
            absent.append("region")
        return tuple(absent)

    @property
    def complete(self) -> bool:
        return not self.missing()

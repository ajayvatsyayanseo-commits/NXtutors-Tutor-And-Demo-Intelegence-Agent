"""The approved WhatsApp template registry.

A template that is not approved in the Meta Business account cannot be delivered
outside the 24-hour session window. Meta does not bounce it loudly — the send
returns an error the caller usually logs and forgets, and the reminder simply
never arrives. So the registry is strict:

* **Names are declared, never constructed.** No f-strings, no
  `f"demo_reminder_{label}"`. A typo must fail at import, not at 02:00 when the
  T-24h reminder is due.
* **Variable count and order are part of the contract.** Meta binds `{{1}}..{{n}}`
  positionally, so a swapped pair sends the tutor's name where the date should
  be. `bind()` refuses a mismatched count.
* **Templates awaiting review are declared and refused.** A template that is
  submitted but not yet Active appears here with `approved=False`, so `bind()`
  raises `TemplateNotApproved` and callers degrade deliberately rather than
  sending something Meta will drop. Flip the flag when it goes Active — do not
  flip it in anticipation.

**This module never creates or renames a template.** Template management is a
Business Manager operation with a review queue; doing it from application code
would mean a deploy could silently change what customers receive.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from demo_command_center.domain.messages import TemplateBinding

# Template *names*, as approved in the WABA. Constants so a typo is an
# ImportError rather than an undeliverable message.
TEMPLATE_REMINDER_T24H = "demo_reminder_t24h"
TEMPLATE_REMINDER_T2H = "demo_reminder_t2h"
TEMPLATE_REMINDER_T15M = "demo_reminder_t15m"
TEMPLATE_TUTOR_REQUEST_EXPIRED = "demo_tutor_request_expired"
TEMPLATE_SCHEDULED_CONFIRMATION = "demo_scheduled_confirmation"
TEMPLATE_CANCELLED = "demo_cancelled"
TEMPLATE_FOLLOWUP = "demo_followup"
#: Confirmed from the WABA template editor on 2026-08-18 (id 2056157498358027).
#: Note `_requested`, not `_request` — the list view truncated it at
#: `demo_tutor_confirmation_rec…` and the obvious guess would have been wrong,
#: which is a send Meta rejects and a tutor who never hears about a booking.
TEMPLATE_TUTOR_CONFIRMATION = "demo_tutor_confirmation_requested"


class TemplateNotApproved(Exception):
    """The template is not usable. Callers degrade; they do not substitute."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"template {name!r} unusable: {reason}")
        self.template = name
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Template:
    """One approved template's contract."""

    name: str
    #: Languages the template is approved in. Meta treats each as a separate
    #: approval, so a Hindi send against an English-only template fails.
    languages: frozenset[str]
    #: Ordered variable names. Documentation *and* the arity check.
    variables: tuple[str, ...]
    category: str = "UTILITY"
    #: False when the exact approved name is not confirmed.
    approved: bool = True
    note: str = ""

    @property
    def arity(self) -> int:
        return len(self.variables)


# Every declaration below was read off the WABA template editor on 2026-08-18.
#
# Two things to hold on to, because both were wrong before that reading and
# neither would have failed a test:
#
#   * NONE of the approved templates carries a name. They are all
#     (date/time, time zone, reference). That is a deliberate privacy choice by
#     whoever authored them — Meta's own editor warns against putting customer
#     information in a template — and it means `student_name` and `tutor_name`
#     have no position to occupy.
#   * They are approved in **English only**. Declaring `hi`/`hinglish` here
#     would make `bind()` accept a language Meta will reject at send time.
#
# The registry is the contract. `_reminder_variables` in handlers/workers.py
# builds its tuple from `variables` below, so these names are not documentation
# — they are the lookup keys, and a rename here changes what is sent.
_TEMPLATES: tuple[Template, ...] = (
    # "Your NXTutors demo is coming up."
    # Date and time: {{1}} · Time zone: {{2}} · Reference: {{3}}
    # "The joining link was sent to you earlier. See you then."
    Template(
        name=TEMPLATE_REMINDER_T24H,
        languages=frozenset({"en"}),
        variables=("demo_datetime", "timezone", "reference"),
    ),
    # "Your NXTutors demo is starting soon."
    # Date and time: {{1}} · Time zone: {{2}} · Reference: {{3}}
    # "Please be ready a few minutes early."
    Template(
        name=TEMPLATE_REMINDER_T2H,
        languages=frozenset({"en"}),
        variables=("demo_datetime", "timezone", "reference"),
    ),
    # "Your NXTutors demo starts in 15 minutes."
    # Date and time: {{1}} · Time zone: {{2}} · Reference: {{3}}
    # "Please join using the link from your confirmation message."
    #
    # Three variables, not two. The previous declaration said (demo_datetime,
    # join_link), which is an arity mismatch — every T-15m reminder would have
    # been refused at send time, silently, fifteen minutes before a demo.
    Template(
        name=TEMPLATE_REMINDER_T15M,
        languages=frozenset({"en"}),
        variables=("demo_datetime", "timezone", "reference"),
    ),
    # "This demo request is no longer available."
    # "The time slot was released because the confirmation window closed."
    # Reference: {{1}}
    # "No action is needed from you."
    #
    # One variable. Sent to the TUTOR, which is why it carries no student name.
    Template(
        name=TEMPLATE_TUTOR_REQUEST_EXPIRED,
        languages=frozenset({"en"}),
        variables=("reference",),
    ),
    # "Your NXTutors demo is confirmed."
    # Date and time: {{1}} · Time zone: {{2}} · Join here: {{3}} · Reference: {{4}}
    # "Please join a few minutes before the start time."
    Template(
        name=TEMPLATE_SCHEDULED_CONFIRMATION,
        languages=frozenset({"en"}),
        variables=("demo_datetime", "timezone", "join_link", "reference"),
    ),
    # "Hello, you have a new NXTutors demo request."
    # Requested time: {{1}} · Time zone: {{2}} · Reference: {{3}}
    # "Please confirm whether you are available for this session."
    # Quick-reply buttons: Accept · Decline
    #
    # This is the message a tutor receives when a parent picks them and asks to
    # book. Their Accept turns into TUTOR_ACCEPTED and the calendar event is
    # created; Decline releases the slot hold and re-matches.
    Template(
        name=TEMPLATE_TUTOR_CONFIRMATION,
        languages=frozenset({"en"}),
        variables=("demo_datetime", "timezone", "reference"),
        note="Quick-reply buttons Accept/Decline. Buttons are not bound as variables.",
    ),
    # "Your NXTutors demo class has been cancelled."
    # Date and time: {{1}} · Time zone: {{2}} · Reference: {{3}}
    # "Reply BOOK and I will find you another slot."
    #
    # 12-hour validity, deliberately: a cancellation that expires undelivered
    # means someone travels to a class that is not happening.
    Template(
        name=TEMPLATE_CANCELLED,
        languages=frozenset({"en"}),
        variables=("demo_datetime", "timezone", "reference"),
        note="Active in the WABA. Verified 2026-08-18 against "
        "GET /{waba}/message_templates: status APPROVED, en, arity confirmed.",
    ),
    # "Thanks for attending your NXTutors demo class."
    # Reference: {{1}}
    # "Reply YES to continue with regular classes, or tell us what you would
    #  like to change."
    Template(
        name=TEMPLATE_FOLLOWUP,
        languages=frozenset({"en"}),
        variables=("reference",),
        note="Active in the WABA. Verified 2026-08-18 against "
        "GET /{waba}/message_templates: status APPROVED, en, arity confirmed.",
    ),
)


class TemplateRegistry:
    def __init__(self, templates: tuple[Template, ...] = _TEMPLATES) -> None:
        self._by_name = {template.name: template for template in templates}

    def get(self, name: str) -> Template:
        template = self._by_name.get(name)
        if template is None:
            raise TemplateNotApproved(name, "not in the registry")
        if not template.approved:
            raise TemplateNotApproved(name, template.note or "approved name unconfirmed")
        return template

    def bind(self, name: str, *, language: str, variables: tuple[str, ...]) -> TemplateBinding:
        """Build a send-ready binding, or raise.

        Arity is checked here rather than at the Meta call because that is the
        difference between a failed send and a wrong one: too few variables is
        an API error, but the *right count in the wrong order* delivers happily
        and reads as nonsense.
        """
        template = self.get(name)
        if language not in template.languages:
            raise TemplateNotApproved(name, f"not approved in language {language!r}")
        if len(variables) != template.arity:
            raise TemplateNotApproved(
                name,
                f"expects {template.arity} variables ({', '.join(template.variables)}), "
                f"got {len(variables)}",
            )
        if any(not value.strip() for value in variables):
            # Meta rejects empty parameters, and a blank in position 2 shifts
            # nothing — it just renders a gap where the date should be.
            raise TemplateNotApproved(name, "template variables must not be empty")
        return TemplateBinding(name=name, language=language, variables=variables)

    def approved_names(self) -> frozenset[str]:
        """What the outbound boundary checks against."""
        return frozenset(t.name for t in self._by_name.values() if t.approved)

    def unconfirmed(self) -> tuple[Template, ...]:
        """Declared but unusable. Reported by `dcc-doctor`."""
        return tuple(t for t in self._by_name.values() if not t.approved)


@lru_cache(maxsize=1)
def registry() -> TemplateRegistry:
    return TemplateRegistry()

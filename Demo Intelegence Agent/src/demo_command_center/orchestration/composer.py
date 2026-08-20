"""Deterministic message composition.

Every customer-facing string this service can produce originates here, from data
it was handed. No model writes a message from scratch; at most one rephrases
something composed by these functions, and the rephrase is checked for invented
numbers before it can be sent.

The rendering rule throughout: **a field we do not have produces no sentence.**
A tutor with no verified fee label gets no fee line — not "fee on request",
which is a claim about their pricing policy that we have not verified either.
"""

from __future__ import annotations

from typing import Any

from demo_command_center.contracts.common import Language
from demo_command_center.contracts.tutor_match import TutorCandidateV1
from demo_command_center.domain.demo import AttendanceSignal, Demo, DemoOutcomeRecord
from demo_command_center.domain.messages import MessageKind, TemplateBinding
from demo_command_center.domain.pricing import ApprovedOffer
from demo_command_center.domain.slots import SlotProposal
from demo_command_center.integrations.meta_whatsapp.templates import (
    TEMPLATE_CANCELLED,
    TEMPLATE_FOLLOWUP,
    TEMPLATE_SCHEDULED_CONFIRMATION,
    TEMPLATE_TUTOR_CONFIRMATION,
    TemplateNotApproved,
    registry,
)

#: What we ask for each missing requirement field. English only for now; the
#: Hindi/Hinglish variants are template-gated and live in the registry.
_QUESTIONS: dict[str, str] = {
    "service": (
        "What kind of tuition are you looking for — school support, "
        "board exam prep, or a competitive exam?"
    ),
    "board": "Which board is the student following (CBSE, ICSE, State Board)?",
    "student_class": "Which class is the student in?",
    "subject": "Which subject would you like the demo class for?",
    "mode": "Would you prefer an online demo or a home tuition demo?",
    "region": "Which area or locality should we look in?",
}


def ask_for(field: str, *, language: Language | None = None) -> str:
    return _QUESTIONS.get(field, "Could you tell me a little more about what you need?")


def tutor_options(candidates: tuple[TutorCandidateV1, ...]) -> str:
    """Present two or three tutors. Only grounded labels are rendered."""
    lines = ["Here are the tutors I would recommend for the demo class:", ""]
    for candidate in candidates:
        lines.append(f"{candidate.rank}. {candidate.name}")
        for reason in candidate.quotable_reasons()[:2]:
            lines.append(f"   • {reason}")
        for label in (
            candidate.availability_label,
            candidate.locality_label,
            candidate.fee_label,
        ):
            if label:
                lines.append(f"   • {label}")
        lines.append(f"   {candidate.profile_url}")
        lines.append("")
    # Two actions, named explicitly. "Know more" needs no agent turn — the
    # profile link above each tutor already carries the full page — so this
    # stays a wording change and adds no state, no trigger and no branch to
    # the machine. A conversational "tell me more about 2" would need all
    # three, and would answer from less information than the page itself.
    ranks = [str(c.rank) for c in candidates]
    choice = f"{', '.join(ranks[:-1])} or {ranks[-1]}" if len(ranks) > 1 else ranks[0]
    lines.append(
        f"Open a tutor's link above to read their full profile, "
        f"or reply {choice} to book a free demo class with them."
    )
    lines.append("Prefer someone else? Say 'show me others'.")
    return "\n".join(lines).strip()


def slot_options(proposals: tuple[SlotProposal, ...]) -> str:
    lines = ["These times work for both of you:", ""]
    lines.extend(f"{p.rank}. {p.slot.label()}" for p in proposals)
    lines.append("")
    lines.append("Reply with the option number, or suggest another time.")
    return "\n".join(lines)


def confirmation(demo: Demo) -> str:
    """The booking confirmation. The Meet link appears only if we have one."""
    if demo.slot is None:
        return "Your demo class is confirmed."
    lines = ["Your demo class is confirmed.", "", f"When: {demo.slot.label()}"]
    if demo.meet_url:
        lines.append(f"Join here: {demo.meet_url}")
    elif demo.location_label:
        lines.append(f"Where: {demo.location_label}")
    lines.extend(["", "You will get reminders before the class. Reply RESCHEDULE to change it."])
    return "\n".join(lines)


def cancellation(demo: Demo | None) -> str:
    if demo is None or demo.slot is None:
        return "Your demo class has been cancelled. Reply BOOK to arrange a new one."
    return (
        f"Your demo class on {demo.slot.label()} has been cancelled.\n\n"
        "Reply BOOK if you would like me to find another slot."
    )


def payment_link(offer: ApprovedOffer, link: str) -> str:
    """Never asks for card details in the chat — only ever links out."""
    lines = [f"Here is your secure payment link for {offer.amount.display()}:", "", link, ""]
    if offer.valid_until is not None:
        lines.append(
            f"The link is valid until {offer.valid_until.strftime('%d %b, %I:%M %p')} UTC."
        )
    lines.append("Never share card or UPI details over chat — always pay on the linked page.")
    return "\n".join(lines)


def welcome() -> str:
    return (
        "Welcome to NXTutors! Your subscription is active.\n\n"
        "Our onboarding team will take it from here and set up your regular classes."
    )


def calendar_summary(demo: Demo) -> str:
    return "NXTutors Demo Class"


def calendar_description(demo: Demo) -> str:
    """No PII. A calendar event body is visible to every invitee."""
    parts = ["NXTutors demo class."]
    if demo.slot is not None:
        parts.append(f"Duration: {demo.slot.duration_minutes} minutes.")
    parts.append(f"Reference: {demo.demo_id}")
    return " ".join(parts)


def tutor_confirmation_template(demo: Demo) -> TemplateBinding | None:
    """The tutor-confirmation template, or None when it is not approved.

    Returns None rather than raising, and rather than falling back to free text.
    The exact approved name for this template was never confirmed (see
    `docs/integration-gaps.md`), so the registry refuses it and the scheduling
    capability degrades to an operator-approval path instead of sending
    something Meta would silently drop.
    """
    if demo.slot is None:
        return None
    try:
        return registry().bind(
            TEMPLATE_TUTOR_CONFIRMATION,
            language=demo.language.value,
            # Requested time · time zone · reference — the order the approved
            # template renders. Duration is not a variable in it; the tutor's
            # Accept/Decline buttons are, and buttons are not bound here.
            variables=(
                demo.slot.label_without_zone(),
                demo.slot.timezone,
                demo.demo_id,
            ),
        )
    except TemplateNotApproved:
        return None


#: Which approved template carries each kind when the 24-hour session window
#: has closed. Reminders and tutor requests are absent because they are
#: `TEMPLATE_REQUIRED` and already bind their own.
_TEMPLATE_FOR_KIND: dict[MessageKind, str] = {
    MessageKind.CONFIRMATION: TEMPLATE_SCHEDULED_CONFIRMATION,
    MessageKind.CANCELLATION: TEMPLATE_CANCELLED,
    MessageKind.FOLLOWUP: TEMPLATE_FOLLOWUP,
}


def window_safe_template(kind: MessageKind, demo: Demo | None) -> TemplateBinding | None:
    """The template that lets this message survive a closed session window.

    Free text is only deliverable within 24 hours of the parent's last message.
    Outside that, Meta drops it and the outbound boundary refuses it as
    `session_window_closed_and_no_template` — correctly, but the message still
    does not arrive.

    Three kinds routinely fall outside the window and were being sent as free
    text alone:

      * a **cancellation**, which can come days after the booking. Dropping it
        leaves someone travelling to a class that is not happening.
      * a **follow-up**, which by definition comes after the demo.
      * a **confirmation** for a reschedule the tutor or an operator initiated,
        where the parent may not have written in a day.

    Attaching a binding makes the message deliverable both ways: the body is
    used inside the window, the template outside it. Returns None when the
    template is not approved yet, so the caller degrades rather than sending
    something Meta will drop.
    """
    name = _TEMPLATE_FOR_KIND.get(kind)
    if name is None:
        return None

    reference = demo.demo_id if demo else ""
    slot = demo.slot if demo else None
    values: dict[str, str] = {
        "demo_datetime": slot.label_without_zone() if slot else "",
        "timezone": slot.timezone if slot else "",
        "join_link": (demo.meet_url or demo.location_label or "") if demo else "",
        "reference": reference,
    }

    try:
        template = registry().get(name)
        return registry().bind(
            name,
            language=(demo.language.value if demo else Language.EN.value),
            # Built from the registry's declared order, never a literal tuple:
            # Meta binds positionally, so a hand-written tuple that drifts by
            # one renders the time zone where the join link belongs.
            variables=tuple(values.get(field, "") or "-" for field in template.variables),
        )
    except TemplateNotApproved:
        return None


def attendance_from_calendar(event: dict[str, Any]) -> DemoOutcomeRecord:
    """Read attendance from calendar RSVP / conference participation only.

    Deliberately narrow. `responseStatus` and a participant count are things
    Google observed; anything else in the event body is something a human typed,
    and the state machine's guard refuses a no-show that rests on it.
    """
    attendees = event.get("attendees") or []
    participants = event.get("participant_count")

    def responded(role: str) -> bool | None:
        for person in attendees:
            if person.get("role") == role:
                status = person.get("responseStatus")
                if status == "accepted":
                    return True
                if status == "declined":
                    return False
                return None
        return None

    tutor_attended = responded("tutor")
    student_attended = responded("student")
    source = AttendanceSignal.CALENDAR_RESPONSE

    if isinstance(participants, int):
        source = AttendanceSignal.MEET_PARTICIPATION
        if participants == 0:
            tutor_attended, student_attended = False, False
        elif participants == 1:
            # Someone joined alone. Which one is unknowable from a count, so we
            # assert nothing rather than guessing and marking the wrong party.
            tutor_attended, student_attended = None, None
        else:
            # Two or more in the room is both parties present. A third joiner
            # (a parent on the student's side) does not change that.
            tutor_attended, student_attended = True, True

    if tutor_attended is None and student_attended is None and not isinstance(participants, int):
        source = AttendanceSignal.NONE

    from demo_command_center.contracts.common import DemoOutcome

    if student_attended and tutor_attended:
        outcome = DemoOutcome.NEUTRAL
    elif student_attended is False or tutor_attended is False:
        outcome = DemoOutcome.NOT_HELD
    else:
        outcome = DemoOutcome.UNKNOWN

    return DemoOutcomeRecord(
        outcome=outcome,
        student_attended=student_attended,
        tutor_attended=tutor_attended,
        evidence_source=source,
        duration_minutes=event.get("duration_minutes"),
    )

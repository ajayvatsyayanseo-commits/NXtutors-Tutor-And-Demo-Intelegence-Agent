"""Stage B — deciding what (if anything) to ask next.

The design constraint is the product one: **do not interrogate the parent.** A
numbered questionnaire is the single fastest way to make an agent feel like a
form. So this module answers a narrow question — what is the *smallest* thing we
still need before a useful match is possible? — and returns at most one question.

Two rules keep it from over-asking:

* a field is only "missing" if its absence would actually change the outcome. A
  parent who said "online" is never asked for their locality;
* anything already known from memory or the lead event is never asked again.
"""

from __future__ import annotations

from dataclasses import dataclass

from tutor_match_meta.contracts.common import MissingField, TuitionMode, Urgency
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.domain import academics, subjects

#: Below this many known blocking fields, matching cannot produce a useful pool.
#: Subject alone is not enough — every city has thousands of maths tutors.
MIN_FIELDS_TO_MATCH = 2


@dataclass(frozen=True, slots=True)
class NextQuestion:
    """One compact question, in the agent's voice."""

    field: str
    text: str
    #: What we already know, echoed back so the parent sees they were heard.
    acknowledgement: str | None = None

    def render(self) -> str:
        if self.acknowledgement:
            return f"{self.acknowledgement} {self.text}"
        return self.text


@dataclass(frozen=True, slots=True)
class Readiness:
    missing: tuple[MissingField, ...]
    ready_to_match: bool
    next_question: NextQuestion | None

    @property
    def blocking_fields(self) -> tuple[str, ...]:
        return tuple(m.field for m in self.missing if m.blocking)


def assess(requirement: MatchRequirementV1) -> Readiness:
    """Work out whether we can match, and what single thing to ask if not."""
    missing = _missing_fields(requirement)
    blocking = [m for m in missing if m.blocking]

    known_count = sum(
        1
        for value in (
            requirement.value_of("subject"),
            requirement.value_of("student_class"),
            requirement.value_of("mode"),
            requirement.location.city or requirement.location.pincode,
        )
        if value
    )
    ready = not blocking and known_count >= MIN_FIELDS_TO_MATCH

    if ready:
        return Readiness(missing=tuple(missing), ready_to_match=True, next_question=None)

    # One question, the highest-priority blocking one.
    target = min(blocking or missing, key=lambda m: m.ask_priority, default=None)
    question = _question_for(target.field, requirement) if target else None
    return Readiness(missing=tuple(missing), ready_to_match=False, next_question=question)


def _missing_fields(requirement: MatchRequirementV1) -> list[MissingField]:
    """Only fields whose absence genuinely changes the match."""
    missing: list[MissingField] = []
    mode = requirement.value_of("mode")
    subject = requirement.value_of("subject")
    grade = academics.class_number(requirement.value_of("student_class"))

    if not subject:
        missing.append(MissingField(field="subject", blocking=True, ask_priority=10))
    elif subjects.is_ambiguous(subject) and grade is not None and grade >= 11:
        # "Science" is one school subject up to Class 10 but three separate ones
        # after, so at senior level the ambiguity is genuinely blocking.
        missing.append(MissingField(field="subject_detail", blocking=True, ask_priority=15))

    if not requirement.value_of("student_class"):
        missing.append(MissingField(field="student_class", blocking=True, ask_priority=20))

    # Location only matters for in-person tuition.
    if mode is not TuitionMode.ONLINE and not (
        requirement.location.city or requirement.location.pincode
    ):
        blocking = mode is TuitionMode.HOME
        missing.append(MissingField(field="location", blocking=blocking, ask_priority=30))

    if mode is None:
        # Not blocking: we can match across modes and let the shortlist show
        # both. Asking is still useful, just not first.
        missing.append(MissingField(field="mode", blocking=False, ask_priority=40))

    # Board only matters where syllabuses actually diverge.
    if not requirement.value_of("board") and academics.board_is_mandatory("CBSE", grade):
        missing.append(MissingField(field="board", blocking=False, ask_priority=50))

    if requirement.preferred_schedule is None:
        missing.append(MissingField(field="schedule", blocking=False, ask_priority=60))

    if not requirement.budget:
        missing.append(MissingField(field="budget", blocking=False, ask_priority=70))

    return missing


def _question_for(field: str, requirement: MatchRequirementV1) -> NextQuestion:
    """One natural question per field, with what we already know echoed back."""
    ack = _acknowledgement(requirement)
    subject = requirement.value_of("subject")
    class_label = requirement.value_of("student_class")

    if field == "subject":
        who = f"for {class_label}" if class_label else "for your child"
        return NextQuestion("subject", f"Which subject do you need help with {who}?", ack)

    if field == "subject_detail":
        parts = sorted(subjects.parts_of(subject or "")) or ["Physics", "Chemistry", "Biology"]
        listed = ", ".join(parts[:-1]) + f" or {parts[-1]}" if len(parts) > 1 else parts[0]
        return NextQuestion("subject", f"Is that {listed}?", ack)

    if field == "student_class":
        what = f"for {subject}" if subject else ""
        return NextQuestion(
            "student_class", f"Which class is your child in{' ' + what if what else ''}?", ack
        )

    if field == "location":
        if requirement.value_of("mode") is TuitionMode.HOME:
            return NextQuestion("location", "Which area should the tutor come to?", ack)
        return NextQuestion("location", "Which city are you in?", ack)

    if field == "mode":
        return NextQuestion("mode", "Would you prefer home tuition or online?", ack)

    if field == "board":
        return NextQuestion("board", "Which board — CBSE, ICSE or something else?", ack)

    if field == "schedule":
        return NextQuestion("schedule", "Which days and times usually work for you?", ack)

    if field == "budget":
        return NextQuestion("budget", "What budget did you have in mind?", ack)

    return NextQuestion(field, "Could you tell me a little more about what you need?", ack)


def _acknowledgement(requirement: MatchRequirementV1) -> str | None:
    """ "Got it — Class 10 CBSE Maths at home in Sector 57."

    Echoing back what was understood does two jobs: it reassures the parent they
    were heard, and it surfaces a misparse immediately instead of three turns
    later when the shortlist is wrong.
    """
    parts: list[str] = []
    if class_label := requirement.value_of("student_class"):
        parts.append(str(class_label))
    if board := requirement.value_of("board"):
        parts.append(str(board))
    if subject := requirement.value_of("subject"):
        parts.append(str(subject))

    if not parts:
        return None

    summary = " ".join(parts)
    mode = requirement.value_of("mode")
    if mode is TuitionMode.HOME:
        summary += " at home"
    elif mode is TuitionMode.ONLINE:
        summary += " online"

    where = requirement.location.locality or requirement.location.city
    if where:
        summary += f" in {where}"

    return f"Got it — {summary}."


def urgency_prefix(requirement: MatchRequirementV1) -> str:
    """A short opener that matches how quickly the parent needs someone."""
    if requirement.urgency is Urgency.URGENT:
        return "Let me find someone who can start right away."
    return ""

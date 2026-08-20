"""What the parent said → which trigger to fire.

The orchestrator takes a `Trigger`, not a sentence. Something has to decide
which trigger an inbound WhatsApp message means, and nothing did: the ingress
handler verified the signature, deduplicated, and dropped the message. This is
that missing half.

Two rules shape the module:

**It is gated on the state machine, not on a table of its own.** Every branch
asks `machine.available(snapshot, actor=...)` before returning a trigger, so it
cannot drift from `state/transitions.py`. Add a transition there and the router
can use it; remove one and it stops offering it. A second copy of "what is
legal from here" would be wrong within a month.

**It refuses rather than guesses.** An unmatched message returns
`trigger=None` with a reason and the caller asks a clarifying question. That is
deliberately not a fallback to "they probably meant yes": every trigger here
moves money, a booking, or a tutor's evening, and a wrong guess on
`slot_agreed` books a stranger's Tuesday.

No model is involved. Keyword and ordinal matching over a closed vocabulary —
the same decision `guardrails/tutor_selection.py` makes, for the same reason:
the input is adversarial and the consequences are real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from demo_command_center.state.machine import StateMachine, StateSnapshot
from demo_command_center.state.triggers import Actor, Trigger

#: Escape hatches, checked before anything positional: a parent who types STOP
#: mid-booking means STOP, not "option 1".
_CANCEL = (
    "cancel", "stop", "unsubscribe", "not interested", "no longer",
    "band karo", "nahi chahiye", "rehne do",
)  # fmt: skip

#: A bare "someone else" is deliberately absent — in front of a shortlist it
#: means another tutor, not another person to talk to, and it lives in
#: `_REJECT_OPTIONS`. "speak to someone else" still lands here via "speak to".
_HUMAN = (
    "human", "agent", "representative", "real person",
    "manager", "complaint", "speak to", "talk to a", "call me",
    "insaan", "baat karni",
)  # fmt: skip

_RESCHEDULE = (
    "reschedule", "postpone", "change the time", "change time", "another time",
    "different time", "move it", "shift", "not free",
    "samay badal", "time badal",
)  # fmt: skip

_REJECT_OPTIONS = (
    "someone else", "other tutor", "others", "different tutor", "more options",
    "alternatives", "none of", "neither", "koi aur", "dusra dikhao",
)  # fmt: skip

_BUTTON_TUTOR = re.compile(r"^tutor:([1-3])$")
_BUTTON_SLOT = re.compile(r"^slot:([1-3])$")

_ORDINAL = re.compile(
    r"\b(?:option\s*)?([1-3])\b|\b(one|two|three|first|second|third|pehla|dusra|teesra)\b"
)
_ORDINAL_WORDS = {
    "one": 1, "first": 1, "pehla": 1,
    "two": 2, "second": 2, "dusra": 2,
    "three": 3, "third": 3, "teesra": 3,
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Routed:
    """A routing decision. `trigger is None` means "ask, do not act"."""

    trigger: Trigger | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    #: Machine-readable, for metrics and for choosing the clarifying reply.
    reason: str = ""

    @property
    def understood(self) -> bool:
        return self.trigger is not None


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _ordinal(text: str) -> int | None:
    match = _ORDINAL.search(text)
    if match is None:
        return None
    if match.group(1):
        return int(match.group(1))
    return _ORDINAL_WORDS.get(match.group(2) or "")


def route(
    *,
    text: str = "",
    button_id: str | None = None,
    snapshot: StateSnapshot,
    machine: StateMachine | None = None,
    actor: Actor = Actor.USER,
) -> Routed:
    """Decide which trigger one inbound message means, if any."""
    engine = machine or StateMachine()
    allowed = engine.available(snapshot, actor=actor)
    lowered = " ".join(text.lower().split())

    def offer(trigger: Trigger, reason: str, **payload: Any) -> Routed | None:
        """Return the trigger only if the machine permits it from here."""
        if trigger not in allowed:
            return None
        return Routed(trigger=trigger, payload={"text": text, **payload}, reason=reason)

    # --- buttons first: an explicit tap needs no parsing ---
    if button_id:
        tapped = button_id.strip().lower()
        found = _BUTTON_TUTOR.match(tapped)
        if found and (
            routed := offer(
                Trigger.TUTOR_CHOSEN, "button_tutor", button_id=tapped, ordinal=int(found.group(1))
            )
        ):
            return routed
        found = _BUTTON_SLOT.match(tapped)
        if found and (
            routed := offer(
                Trigger.SLOT_AGREED, "button_slot", button_id=tapped, ordinal=int(found.group(1))
            )
        ):
            return routed
        return Routed(reason=f"button_not_valid_here:{tapped[:24]}")

    if not lowered:
        return Routed(reason="empty_message")

    # --- escape hatches, before anything positional ---
    if _has(lowered, _HUMAN) and (routed := offer(Trigger.HUMAN_REQUESTED, "asked_for_a_human")):
        return routed
    if _has(lowered, _CANCEL) and (routed := offer(Trigger.CANCELLED_BY_USER, "asked_to_cancel")):
        return routed

    # Before reschedule: "show me other tutors" is not "change my demo time",
    # and the two vocabularies overlap.
    if _has(lowered, _REJECT_OPTIONS) and (
        routed := offer(Trigger.OPTIONS_REJECTED, "rejected_the_shortlist")
    ):
        return routed

    if _has(lowered, _RESCHEDULE) and (
        routed := offer(Trigger.RESCHEDULE_REQUESTED, "asked_to_reschedule")
    ):
        return routed

    # --- positional: an ordinal means whichever list we last showed ---
    position = _ordinal(lowered)
    if position is not None:
        for trigger, reason in (
            (Trigger.TUTOR_CHOSEN, "ordinal_tutor"),
            (Trigger.SLOT_AGREED, "ordinal_slot"),
        ):
            if routed := offer(trigger, reason, ordinal=position):
                return routed

    # A name, in the state where a name is what we are waiting for. Resolution
    # against the persisted snapshot happens downstream in
    # `guardrails.tutor_selection`; this only decides the message *is* a
    # selection attempt.
    if Trigger.TUTOR_CHOSEN in allowed and re.search(r"[a-z]{3,}", lowered):
        return Routed(
            trigger=Trigger.TUTOR_CHOSEN, payload={"text": text}, reason="possible_name_selection"
        )

    # Anything else while requirements are still being gathered is a requirement.
    if routed := offer(Trigger.REQUIREMENTS_UPDATED, "requirement_text"):
        return routed

    return Routed(reason=f"no_trigger_for_state:{snapshot.state.value}")


def clarification(routed: Routed, snapshot: StateSnapshot) -> str:
    """What to say when we did not understand. Never a guess at the intent."""
    from demo_command_center.state.states import DemoState

    if routed.reason.startswith("button_not_valid_here"):
        return "That option has expired. Could you tell me in words what you would like to do?"
    if routed.reason == "empty_message":
        return "Sorry, I did not catch that — could you send it again?"

    asking = {
        DemoState.AWAITING_TUTOR_SELECTION: (
            "Reply 1, 2 or 3 to pick a tutor — or say 'show me others'."
        ),
        DemoState.NEGOTIATING_SLOT: "Reply with the option number, or suggest another time.",
        DemoState.TUTOR_SELECTED: "Reply with the option number, or suggest another time.",
        DemoState.COLLECTING_REQUIREMENTS: (
            "Could you tell me the subject, class and board you need help with?"
        ),
        DemoState.PAYMENT_PENDING: (
            "Your payment link is above. Reply CANCEL to stop, or ask for a human."
        ),
    }
    return asking.get(
        snapshot.state,
        "I did not quite follow. You can reply CANCEL to stop, or ask to speak to a human.",
    )


# ---------------------------------------------------------------------------
# Requirement capture
#
# `ASK_MISSING_REQUIREMENTS` asks for the first missing field; something has to
# put the answer back, and nothing did — so the agent asked the same question
# forever. Deliberately deterministic: `service` selects which Tutor scoring
# policy ranks the shortlist, and `board` and `student_class` are hard filters.
# A model that guesses "CBSE" from a parent who said "state board" does not
# produce a worse sentence, it produces the wrong tutors ranked by the wrong
# weights, with no error anywhere.
#
# Unrecognised text leaves the field `None`, so `missing()` keeps asking rather
# than proceeding on a guess.
# ---------------------------------------------------------------------------

_SUBJECTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("maths", "math", "mathematics", "ganit"), "Mathematics"),
    (("physics",), "Physics"),
    (("chemistry",), "Chemistry"),
    (("biology", "bio"), "Biology"),
    (("science",), "Science"),
    (("english",), "English"),
    (("hindi",), "Hindi"),
    (("social", "sst", "history", "geography", "civics"), "Social Studies"),
    (("accounts", "accountancy"), "Accountancy"),
    (("economics", "eco"), "Economics"),
    (("computer", "coding", "programming"), "Computer Science"),
)

_BOARDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("cbse",), "CBSE"),
    (("icse", "isc"), "ICSE"),
    (("igcse",), "IGCSE"),
    (("ib ", "international baccalaureate"), "IB"),
    (("state board", "state-board", "stateboard"), "State Board"),
)

#: Maps to a Tutor scoring policy name. Order matters: a competitive exam beats
#: the generic school-support fallback.
_SERVICES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("jee", "neet", "olympiad", "competitive", "entrance"), "competitive_exam"),
    (("board exam", "boards", "board prep", "board preparation"), "board_exam_prep"),
    (("urgent", "asap", "immediately", "kal se", "turant"), "urgent_tuition"),
    (("school", "regular", "tuition", "homework", "weak in"), "regular_school_support"),
)

_CLASS = re.compile(r"\b(?:class|grade|std|standard)\s*([1-9]|1[0-2])\b|\b([1-9]|1[0-2])\s*th\b")


def capture(text: str, current: Any) -> Any:
    """Fold what the parent said into the requirement. Returns a new one.

    `current` is a `Requirement`; typed `Any` to keep this module free of a
    contracts import cycle. Only ever adds — a field already answered is never
    rewritten by a later mention, so "my nephew does ICSE" cannot overwrite a
    board the parent already confirmed.
    """
    lowered = " ".join(text.lower().split())
    if not lowered:
        return current

    found: dict[str, Any] = {}

    if current.subject is None:
        for words, canonical in _SUBJECTS:
            if _has(lowered, words):
                found["subject"] = canonical
                break

    if current.board is None:
        for words, canonical in _BOARDS:
            if _has(lowered, words):
                found["board"] = canonical
                break

    if current.student_class is None and (match := _CLASS.search(lowered)):
        found["student_class"] = match.group(1) or match.group(2)

    if current.mode is None:
        from demo_command_center.contracts.common import DemoMode

        if _has(lowered, ("online", "video", "zoom", "remote")):
            found["mode"] = DemoMode.ONLINE
        elif _has(lowered, ("home", "at my place", "offline", "in person", "ghar")):
            found["mode"] = DemoMode.HOME

    if current.service is None:
        for words, canonical in _SERVICES:
            if _has(lowered, words):
                found["service"] = canonical
                break

    return current.model_copy(update=found) if found else current

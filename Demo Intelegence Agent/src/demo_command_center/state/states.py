"""The conversation lifecycle states and their classification.

A closed enum. The LLM may interpret text, but the value of this enum is only
ever assigned by a transition in `state/transitions.py` — which is what makes
"a model decided the customer had paid" structurally impossible rather than
merely discouraged.

States are grouped by three orthogonal questions the rest of the system asks:

* `TERMINAL`   — is this conversation over? A new inbound message starts a new
  one instead of resuming a closed lifecycle.
* `AWAITING_USER` — are we blocked on the parent? Reminder and nudge policy
  only apply here; nudging someone while we are waiting on Google is noise.
* `SUSPENDED`  — has a human or an external system taken over? No automated
  message may be sent from these, regardless of what any capability proposes.
"""

from __future__ import annotations

from enum import StrEnum


class DemoState(StrEnum):
    """The demo lifecycle. Ordered roughly by funnel position, not enforced."""

    NEW = "NEW"
    OWNERSHIP_ACQUIRING = "OWNERSHIP_ACQUIRING"
    IDENTITY_RESOLUTION = "IDENTITY_RESOLUTION"
    COLLECTING_REQUIREMENTS = "COLLECTING_REQUIREMENTS"

    TUTOR_MATCH_REQUESTED = "TUTOR_MATCH_REQUESTED"
    TUTOR_OPTIONS_READY = "TUTOR_OPTIONS_READY"
    AWAITING_TUTOR_SELECTION = "AWAITING_TUTOR_SELECTION"
    TUTOR_SELECTED = "TUTOR_SELECTED"
    TUTOR_CONFIRMATION_PENDING = "TUTOR_CONFIRMATION_PENDING"

    NEGOTIATING_SLOT = "NEGOTIATING_SLOT"
    SLOT_HELD = "SLOT_HELD"
    CALENDAR_CREATION_PENDING = "CALENDAR_CREATION_PENDING"
    SCHEDULED = "SCHEDULED"
    REMINDERS_ACTIVE = "REMINDERS_ACTIVE"
    DEMO_READY = "DEMO_READY"

    OUTCOME_PENDING = "OUTCOME_PENDING"
    COMPLETED = "COMPLETED"
    STUDENT_NO_SHOW_REVIEW = "STUDENT_NO_SHOW_REVIEW"
    TUTOR_NO_SHOW_REVIEW = "TUTOR_NO_SHOW_REVIEW"

    POST_DEMO_ANALYSIS = "POST_DEMO_ANALYSIS"
    FOLLOWUP_PENDING = "FOLLOWUP_PENDING"

    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    SUBSCRIPTION_ACTIVATING = "SUBSCRIPTION_ACTIVATING"
    ONBOARDING_HANDOFF_PENDING = "ONBOARDING_HANDOFF_PENDING"
    CONVERTED = "CONVERTED"

    CANCELLED = "CANCELLED"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"


#: Nothing resumes from here. A new inbound message opens a new conversation.
TERMINAL: frozenset[DemoState] = frozenset(
    {
        DemoState.CONVERTED,
        DemoState.CANCELLED,
        DemoState.FAILED_TERMINAL,
    }
)

#: We are blocked on the parent. Only these are eligible for a nudge.
AWAITING_USER: frozenset[DemoState] = frozenset(
    {
        DemoState.COLLECTING_REQUIREMENTS,
        DemoState.AWAITING_TUTOR_SELECTION,
        DemoState.NEGOTIATING_SLOT,
        DemoState.FOLLOWUP_PENDING,
        DemoState.PAYMENT_PENDING,
    }
)

#: A human holds the conversation. The automated outbound path refuses to send
#: from these even when a capability asks it to.
#:
#: `ONBOARDING_HANDOFF_PENDING` is deliberately NOT here. "We have asked
#: Onboarding to take over" is not the same as "Onboarding has it": we still own
#: the conversation until they accept, and we still owe the customer a welcome
#: message. Ownership moves on acceptance, not on request.
SUSPENDED: frozenset[DemoState] = frozenset({DemoState.HUMAN_HANDOFF})

#: A demo exists and is on the calendar. Reminders and no-show detection key off
#: this set rather than testing three states at every call site.
DEMO_BOOKED: frozenset[DemoState] = frozenset(
    {
        DemoState.SCHEDULED,
        DemoState.REMINDERS_ACTIVE,
        DemoState.DEMO_READY,
    }
)

#: The demo has happened (or definitively has not) and post-demo work applies.
POST_DEMO: frozenset[DemoState] = frozenset(
    {
        DemoState.COMPLETED,
        DemoState.POST_DEMO_ANALYSIS,
        DemoState.FOLLOWUP_PENDING,
        DemoState.STUDENT_NO_SHOW_REVIEW,
        DemoState.TUTOR_NO_SHOW_REVIEW,
    }
)

#: Money is in flight. Duplicate-payment protection and reconciliation apply.
PAYMENT_IN_FLIGHT: frozenset[DemoState] = frozenset(
    {
        DemoState.PAYMENT_PENDING,
        DemoState.PAYMENT_CONFIRMED,
        DemoState.SUBSCRIPTION_ACTIVATING,
    }
)


def is_terminal(state: DemoState) -> bool:
    return state in TERMINAL


def can_send_automated_message(state: DemoState) -> bool:
    """Whether the outbound boundary may send an ordinary business message.

    Terminal *and* suspended both block. Suspended is the subtle one: a
    capability worker that finished late must not talk over the human who took
    the conversation while it was running.
    """
    return state not in TERMINAL and state not in SUSPENDED

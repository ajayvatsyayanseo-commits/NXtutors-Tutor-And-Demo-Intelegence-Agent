"""What can move the conversation, and who is allowed to say it.

Separating the trigger from the actor is the authorization model. `PAYMENT_PAID`
is a real trigger, but only `Actor.PAYMENT_PROVIDER` may fire it — so a parent
typing "I have paid", or a model summarising that they said so, cannot advance
the conversation past the money.
"""

from __future__ import annotations

from enum import StrEnum


class Actor(StrEnum):
    """Who is asking for a transition. Checked against every transition rule."""

    #: The parent/student on WhatsApp. Never trusted for anything commercial.
    USER = "user"
    #: The tutor, replying to a confirmation request.
    TUTOR = "tutor"
    #: An internal capability worker running under our own authority.
    SYSTEM = "system"
    #: A verified server-to-server Cashfree webhook. Never a user claim.
    PAYMENT_PROVIDER = "payment_provider"
    #: A verified handoff envelope from another NXTutors agent.
    AGENT = "agent"
    #: An authenticated staff member in the ops console.
    OPERATOR = "operator"
    #: A time-based trigger from EventBridge Scheduler.
    SCHEDULER = "scheduler"


class Trigger(StrEnum):
    """Every event that may cause a state transition."""

    # --- intake
    HANDOFF_RECEIVED = "handoff_received"
    OWNERSHIP_ACQUIRED = "ownership_acquired"
    IDENTITY_RESOLVED = "identity_resolved"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    REQUIREMENTS_UPDATED = "requirements_updated"
    REQUIREMENTS_COMPLETE = "requirements_complete"

    # --- tutor discovery
    MATCH_REQUESTED = "match_requested"
    MATCH_SUCCEEDED = "match_succeeded"
    MATCH_EMPTY = "match_empty"
    OPTIONS_PRESENTED = "options_presented"
    TUTOR_CHOSEN = "tutor_chosen"
    OPTIONS_REJECTED = "options_rejected"

    # --- tutor confirmation
    TUTOR_CONFIRMATION_SENT = "tutor_confirmation_sent"
    TUTOR_ACCEPTED = "tutor_accepted"
    TUTOR_DECLINED = "tutor_declined"
    TUTOR_REQUEST_EXPIRED = "tutor_request_expired"

    # --- scheduling
    SLOT_PROPOSED = "slot_proposed"
    SLOT_AGREED = "slot_agreed"
    HOLD_PLACED = "hold_placed"
    HOLD_EXPIRED = "hold_expired"
    CALENDAR_REQUESTED = "calendar_requested"
    CALENDAR_CREATED = "calendar_created"
    CALENDAR_FAILED = "calendar_failed"
    REMINDERS_SCHEDULED = "reminders_scheduled"
    DEMO_WINDOW_OPEN = "demo_window_open"
    RESCHEDULE_REQUESTED = "reschedule_requested"
    CANCELLED_BY_USER = "cancelled_by_user"
    CANCELLED_BY_TUTOR = "cancelled_by_tutor"

    # --- outcome
    OUTCOME_DUE = "outcome_due"
    DEMO_COMPLETED = "demo_completed"
    STUDENT_ABSENT = "student_absent"
    TUTOR_ABSENT = "tutor_absent"
    NO_SHOW_RESOLVED = "no_show_resolved"

    # --- post-demo
    ANALYSIS_REQUESTED = "analysis_requested"
    ANALYSIS_COMPLETE = "analysis_complete"
    FOLLOWUP_SENT = "followup_sent"

    # --- commercial
    PAYMENT_LINK_ISSUED = "payment_link_issued"
    PAYMENT_PAID = "payment_paid"
    PAYMENT_FAILED = "payment_failed"
    ACTIVATION_STARTED = "activation_started"
    ACTIVATION_SUCCEEDED = "activation_succeeded"
    ACTIVATION_FAILED = "activation_failed"
    ONBOARDING_HANDED_OFF = "onboarding_handed_off"
    ONBOARDING_ACCEPTED = "onboarding_accepted"

    # --- exceptional
    HUMAN_REQUESTED = "human_requested"
    HUMAN_RESOLVED = "human_resolved"
    RECOVERABLE_FAILURE = "recoverable_failure"
    RETRY = "retry"
    ABANDON = "abandon"


#: Triggers a parent may never fire, whatever they type and whatever a model
#: infers from it. Enforced in `transitions.py`, so it holds for every caller.
USER_FORBIDDEN: frozenset[Trigger] = frozenset(
    {
        Trigger.PAYMENT_PAID,
        Trigger.PAYMENT_FAILED,
        Trigger.ACTIVATION_SUCCEEDED,
        Trigger.ACTIVATION_FAILED,
        Trigger.TUTOR_ACCEPTED,
        Trigger.TUTOR_DECLINED,
        Trigger.DEMO_COMPLETED,
        Trigger.STUDENT_ABSENT,
        Trigger.TUTOR_ABSENT,
        Trigger.MATCH_SUCCEEDED,
        Trigger.IDENTITY_RESOLVED,
    }
)

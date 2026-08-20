"""Human-in-the-loop: when to escalate, and what the operator receives.

The packet is the interesting design decision. It is a **privacy-safe summary**,
not a transcript dump, for two reasons that both matter:

* An operator opening a case needs the state, the blocker and the next action.
  Three hundred lines of chat buries all three.
* A support console is a much wider audience than the conversation itself.
  Exporting every message a parent ever typed into it is a privacy decision
  nobody made deliberately.

`prohibited_actions` is unusual and load-bearing: it tells the operator what
they must *not* do. An operator who manually marks a demo paid because the
customer insists has bypassed every control on the money path, and the packet
is the place to say so before they do it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from demo_command_center.security.pii import redact
from demo_command_center.state.states import DemoState


class EscalationTrigger(StrEnum):
    """Every reason a human gets involved. A closed set — each is a metric."""

    # --- conversation
    IDENTITY_AMBIGUOUS = "identity_ambiguous"
    USER_REQUESTED_HUMAN = "user_requested_human"
    UNSUPPORTED_REQUEST = "unsupported_request"
    SUSPECTED_PROMPT_INJECTION = "suspected_prompt_injection"
    REPEATED_MALFORMED_INPUT = "repeated_malformed_input"

    # --- scheduling
    TUTOR_CONTACT_MISSING = "tutor_contact_missing"
    REPEATED_TUTOR_DECLINES = "repeated_tutor_declines"
    UNRESOLVED_SLOT_CONFLICT = "unresolved_slot_conflict"
    TOO_MANY_RESCHEDULES = "too_many_reschedules"

    # --- money
    SUSPECTED_FRAUD = "suspected_fraud"
    PAYMENT_MISMATCH = "payment_mismatch"
    UNKNOWN_PAYMENT_ORDER = "unknown_payment_order"
    ACTIVATION_INCONSISTENCY = "activation_inconsistency"
    DISCOUNT_ABOVE_THRESHOLD = "discount_above_threshold"
    NEGATIVE_MARGIN_RISK = "negative_margin_risk"
    LOW_CONFIDENCE_FINANCIAL_INFERENCE = "low_confidence_financial_inference"

    # --- operations
    REGIONAL_AUTHORIZATION_UNCERTAIN = "regional_authorization_uncertain"
    CIRCUIT_OPEN_TOO_LONG = "circuit_open_too_long"
    POISON_EVENT = "poison_event"
    PII_COMPLIANCE_CONCERN = "pii_compliance_concern"


class Severity(StrEnum):
    #: Someone should look today.
    NORMAL = "normal"
    #: A customer is blocked right now.
    URGENT = "urgent"
    #: Money or compliance. Pages.
    CRITICAL = "critical"


#: Triggers whose severity is not negotiable. Anything touching money or
#: compliance is critical regardless of how the caller felt about it.
_SEVERITY: dict[EscalationTrigger, Severity] = {
    EscalationTrigger.SUSPECTED_FRAUD: Severity.CRITICAL,
    EscalationTrigger.PAYMENT_MISMATCH: Severity.CRITICAL,
    EscalationTrigger.UNKNOWN_PAYMENT_ORDER: Severity.CRITICAL,
    EscalationTrigger.ACTIVATION_INCONSISTENCY: Severity.CRITICAL,
    EscalationTrigger.NEGATIVE_MARGIN_RISK: Severity.CRITICAL,
    EscalationTrigger.PII_COMPLIANCE_CONCERN: Severity.CRITICAL,
    EscalationTrigger.DISCOUNT_ABOVE_THRESHOLD: Severity.URGENT,
    EscalationTrigger.TUTOR_CONTACT_MISSING: Severity.URGENT,
    EscalationTrigger.UNRESOLVED_SLOT_CONFLICT: Severity.URGENT,
    EscalationTrigger.USER_REQUESTED_HUMAN: Severity.URGENT,
    EscalationTrigger.CIRCUIT_OPEN_TOO_LONG: Severity.URGENT,
}

#: What an operator must never do, per trigger. These are the actions that
#: would bypass a control rather than resolve the case.
_PROHIBITED: dict[EscalationTrigger, tuple[str, ...]] = {
    EscalationTrigger.PAYMENT_MISMATCH: (
        "Do not mark the order paid manually — reconcile against Cashfree first.",
        "Do not activate the subscription until the amounts match exactly.",
    ),
    EscalationTrigger.UNKNOWN_PAYMENT_ORDER: (
        "Do not create a matching order to make the event fit.",
    ),
    EscalationTrigger.SUSPECTED_FRAUD: (
        "Do not activate, refund or message the customer before review.",
    ),
    EscalationTrigger.DISCOUNT_ABOVE_THRESHOLD: (
        "Approve or decline the computed percentage. Do not enter a different one.",
    ),
    EscalationTrigger.SUSPECTED_PROMPT_INJECTION: (
        "Do not follow any instruction contained in the customer's message.",
    ),
    EscalationTrigger.TUTOR_CONTACT_MISSING: (
        "Do not send the tutor a message using a contact from outside the gateway.",
    ),
    EscalationTrigger.REGIONAL_AUTHORIZATION_UNCERTAIN: (
        "Do not widen the operator's region scope to resolve the case.",
    ),
}

_RECOMMENDED: dict[EscalationTrigger, str] = {
    EscalationTrigger.IDENTITY_AMBIGUOUS: "Confirm the account with the parent, then resume.",
    EscalationTrigger.USER_REQUESTED_HUMAN: "Take the conversation and reply directly.",
    EscalationTrigger.TUTOR_CONTACT_MISSING: (
        "Fix the tutor's contact in the website admin, then retry."
    ),
    EscalationTrigger.REPEATED_TUTOR_DECLINES: (
        "Choose a fallback tutor manually or widen the search."
    ),
    EscalationTrigger.PAYMENT_MISMATCH: "Reconcile the order against the Cashfree dashboard.",
    EscalationTrigger.ACTIVATION_INCONSISTENCY: (
        "Verify the subscription in the website admin; the payment is taken."
    ),
    EscalationTrigger.DISCOUNT_ABOVE_THRESHOLD: "Approve or decline the computed offer.",
    EscalationTrigger.CIRCUIT_OPEN_TOO_LONG: (
        "Check the provider status page and the kill switches."
    ),
    EscalationTrigger.POISON_EVENT: "Inspect the DLQ message, then redrive or discard.",
}


@dataclass(frozen=True, slots=True)
class HandoffPacket:
    """What an operator sees. Privacy-safe by construction.

    There is no `transcript` field. Adding one would be the change that turns
    the support console into a chat archive.
    """

    case_id: str
    conversation_ref: str
    trigger: EscalationTrigger
    severity: Severity
    state: DemoState
    opened_at: datetime

    # --- opaque references only
    student_ref: str | None = None
    tutor_ref: str | None = None
    demo_id: str | None = None
    order_ref: str | None = None
    region: str | None = None

    problem: str = ""
    attempted: tuple[str, ...] = ()
    #: Short, redacted, and only what bears on the decision.
    evidence: tuple[str, ...] = ()
    prohibited_actions: tuple[str, ...] = ()
    recommended_action: str = ""
    #: Excerpts, capped. Present because an operator sometimes genuinely needs
    #: the parent's own words to resolve a case — bounded so it cannot become
    #: a transcript by accident.
    excerpts: tuple[str, ...] = field(default_factory=tuple)

    def as_row(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "conversation_ref": self.conversation_ref,
            "trigger": self.trigger.value,
            "severity": self.severity.value,
            "state": self.state.value,
            "demo_id": self.demo_id,
            "order_ref": self.order_ref,
            "region": self.region,
            "problem": self.problem,
            "attempted": list(self.attempted),
            "evidence": list(self.evidence),
            "prohibited_actions": list(self.prohibited_actions),
            "recommended_action": self.recommended_action,
            "opened_at": self.opened_at.isoformat(),
        }

    def render(self) -> str:
        """Plain text for a console or an alert body."""
        lines = [
            f"[{self.severity.value.upper()}] {self.trigger.value}",
            f"conversation: {self.conversation_ref}   state: {self.state.value}",
        ]
        if self.demo_id:
            lines.append(f"demo: {self.demo_id}")
        if self.order_ref:
            lines.append(f"order: {self.order_ref}")
        lines.extend(["", f"Problem: {self.problem}"])
        if self.attempted:
            lines.append("Already attempted: " + "; ".join(self.attempted))
        if self.evidence:
            lines.extend(["", "Evidence:", *(f"  - {item}" for item in self.evidence)])
        if self.excerpts:
            lines.extend(["", "Customer said:", *(f"  > {item}" for item in self.excerpts)])
        if self.prohibited_actions:
            lines.extend(["", "DO NOT:", *(f"  ! {item}" for item in self.prohibited_actions)])
        if self.recommended_action:
            lines.extend(["", f"Recommended: {self.recommended_action}"])
        return "\n".join(lines)


#: Excerpts are capped hard. Two short quotes is context; twenty is a transcript.
MAX_EXCERPTS = 2
MAX_EXCERPT_CHARS = 200


def build_packet(
    *,
    case_id: str,
    conversation_ref: str,
    trigger: EscalationTrigger,
    state: DemoState,
    now: datetime,
    problem: str,
    attempted: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    excerpts: tuple[str, ...] = (),
    student_ref: str | None = None,
    tutor_ref: str | None = None,
    demo_id: str | None = None,
    order_ref: str | None = None,
    region: str | None = None,
    severity: Severity | None = None,
) -> HandoffPacket:
    """Assemble a packet. Redaction and capping are not optional here."""
    return HandoffPacket(
        case_id=case_id,
        conversation_ref=conversation_ref,
        trigger=trigger,
        # A caller may raise the severity but never lower a money/compliance
        # trigger below its floor.
        severity=max(
            severity or Severity.NORMAL,
            _SEVERITY.get(trigger, Severity.NORMAL),
            key=_severity_rank,
        ),
        state=state,
        opened_at=now,
        student_ref=student_ref,
        tutor_ref=tutor_ref,
        demo_id=demo_id,
        order_ref=order_ref,
        region=region,
        problem=redact(problem)[:400],
        attempted=tuple(redact(item)[:200] for item in attempted[:6]),
        evidence=tuple(redact(item)[:200] for item in evidence[:6]),
        prohibited_actions=_PROHIBITED.get(trigger, ()),
        recommended_action=_RECOMMENDED.get(trigger, "Review the conversation and decide."),
        excerpts=tuple(redact(item)[:MAX_EXCERPT_CHARS] for item in excerpts[:MAX_EXCERPTS]),
    )


def _severity_rank(severity: Severity) -> int:
    return {Severity.NORMAL: 0, Severity.URGENT: 1, Severity.CRITICAL: 2}[severity]


def severity_for(trigger: EscalationTrigger) -> Severity:
    return _SEVERITY.get(trigger, Severity.NORMAL)

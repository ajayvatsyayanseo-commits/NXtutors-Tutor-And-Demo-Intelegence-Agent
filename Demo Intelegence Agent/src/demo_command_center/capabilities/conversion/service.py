"""Capability 032 — Post-Demo Conversion.

Composes the follow-up message. The interesting constraint is what it is
*allowed* to say: a `ConversionFacts` object is assembled from verified sources
only, and the composer can render nothing that is not on it. A tutor's name
comes from the candidate snapshot, the price from the gateway quote, the offer
from an `ApprovedOffer` — there is no code path that turns a model's suggestion
into a claim.

Urgency is the same rule applied to time. `deadline` is either a real
`valid_until` from an approved offer or it is absent, and when it is absent the
message says nothing about time running out. Manufactured scarcity is the
easiest thing for a language model to produce and the fastest way to lose trust.

The LLM's role is optional polish: it may rephrase a composed message into
warmer Hinglish, and its output is checked to contain no fact the deterministic
version did not already contain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from demo_command_center.contracts.common import DemoOutcome, Language
from demo_command_center.domain.objections import NextStep, ObjectionAnalysisV1
from demo_command_center.domain.pricing import ApprovedOffer, PlanQuote
from demo_command_center.shared.money import Money


@dataclass(frozen=True, slots=True)
class ConversionFacts:
    """Everything the follow-up may mention. Nothing else is available to it."""

    student_name: str = ""
    tutor_name: str = ""
    subject: str = ""
    learning_goal: str = ""
    outcome: DemoOutcome = DemoOutcome.UNKNOWN
    #: Verbatim tutor strengths from the Tutor Intelligence evidence. Already
    #: filtered to quotable dimensions upstream.
    tutor_strengths: tuple[str, ...] = ()
    quote: PlanQuote | None = None
    offer: ApprovedOffer | None = None
    #: Genuine social proof only: a real count from the gateway, or nothing.
    tutor_demo_count: int | None = None
    tutor_rating: float | None = None
    language: Language = Language.EN

    @property
    def deadline(self) -> datetime | None:
        """Real expiry or None. There is no third option."""
        return self.offer.valid_until if self.offer else None


@dataclass(slots=True)
class ComposedFollowup:
    body: str
    #: Which facts were actually used. The guardrail checks the rendered text
    #: against this, so a claim with no backing fact is detectable.
    used_facts: list[str] = field(default_factory=list)
    next_step: NextStep = NextStep.SEND_FOLLOWUP


class ConversionCapability:
    """Deterministic composition. The model, if used, only rephrases."""

    def compose(
        self,
        *,
        facts: ConversionFacts,
        analysis: ObjectionAnalysisV1 | None,
        strategy_hint: NextStep | None = None,
        now: datetime,
    ) -> ComposedFollowup:
        lines: list[str] = []
        used: list[str] = []

        greeting = f"Hi {facts.student_name}," if facts.student_name else "Hi,"
        lines.append(greeting)
        if facts.student_name:
            used.append("student_name")

        lines.append(self._opening(facts, used))

        strength = self._strength_line(facts, used)
        if strength:
            lines.append(strength)

        objection_line = self._objection_line(analysis, facts, used)
        if objection_line:
            lines.append(objection_line)

        price_line = self._price_line(facts, used, now=now)
        if price_line:
            lines.append(price_line)

        lines.append(self._call_to_action(facts))

        return ComposedFollowup(
            body="\n\n".join(line for line in lines if line),
            used_facts=used,
            next_step=strategy_hint
            or (analysis.recommended_next_step if analysis else NextStep.SEND_FOLLOWUP),
        )

    # -------------------------------------------------------------- sections
    def _opening(self, facts: ConversionFacts, used: list[str]) -> str:
        """References the demo that actually happened, or stays generic."""
        if facts.outcome is DemoOutcome.NOT_HELD:
            return "Sorry we missed each other for the demo class."
        subject = f" {facts.subject}" if facts.subject else ""
        tutor = f" with {facts.tutor_name}" if facts.tutor_name else ""
        if facts.subject:
            used.append("subject")
        if facts.tutor_name:
            used.append("tutor_name")
        return f"Thanks for joining the{subject} demo class{tutor}."

    def _strength_line(self, facts: ConversionFacts, used: list[str]) -> str:
        """Only evidence-backed strengths, and only real social proof."""
        parts: list[str] = []
        if facts.tutor_strengths:
            used.append("tutor_strengths")
            parts.append(facts.tutor_strengths[0])
        if facts.tutor_demo_count is not None and facts.tutor_demo_count >= 10:
            used.append("tutor_demo_count")
            parts.append(f"has taught {facts.tutor_demo_count} demo classes on NXTutors")
        if facts.tutor_rating is not None:
            used.append("tutor_rating")
            parts.append(f"is rated {facts.tutor_rating:.1f}/5 by parents")
        if not parts:
            return ""
        name = facts.tutor_name or "Your tutor"
        return (
            f"{name} {'; '.join(parts)}."
            if not facts.tutor_strengths
            else f"{name}: {'; '.join(parts)}."
        )

    def _objection_line(
        self, analysis: ObjectionAnalysisV1 | None, facts: ConversionFacts, used: list[str]
    ) -> str:
        """Address only what the parent actually said, in their own words.

        Uses `customer_quotable()`, so an inferred objection informs the
        strategy but is never echoed back as something they said.
        """
        if analysis is None:
            return ""
        quotable = analysis.customer_quotable()
        if not quotable:
            return ""
        used.append("objection")
        top = quotable[0]
        goal = f" for {facts.learning_goal}" if facts.learning_goal else ""
        if facts.learning_goal:
            used.append("learning_goal")
        return (
            f"You mentioned: “{top.quote.strip()}” — happy to work through that"
            f"{goal} before you decide."
        )

    def _price_line(self, facts: ConversionFacts, used: list[str], *, now: datetime) -> str:
        """Renders only an already-approved offer, or the plain list price."""
        if facts.offer is not None and facts.offer.live(now=now):
            used.extend(["offer", "list_price"])
            amount = facts.offer.amount
            line = (
                f"Your plan works out to {amount.display()}"
                f" (down from {Money(facts.offer.list_price_minor, facts.offer.currency).display()}"
                f", {facts.offer.discount_percent}% off)."
            )
            if facts.deadline is not None:
                used.append("deadline")
                line += f" This price holds until {facts.deadline.strftime('%d %b, %I:%M %p')} UTC."
            return line
        if facts.quote is not None:
            used.append("list_price")
            return f"The {facts.quote.plan_name} plan is {facts.quote.list_price.display()}."
        return ""

    @staticmethod
    def _call_to_action(facts: ConversionFacts) -> str:
        if facts.outcome is DemoOutcome.NOT_HELD:
            return "Would you like me to rebook the demo? Reply YES and I will find a new slot."
        return "Reply YES to continue, or tell me what you would like to change."


def rephrase_is_safe(original: ComposedFollowup, rewritten: str) -> tuple[str, ...]:
    """Claims in the rewrite that the deterministic version never made.

    Deliberately narrow and mechanical: it looks for numbers, currency and
    percentages that were not in the original. A model asked to warm up the tone
    that comes back with a different price or a new "only 2 seats left" is
    caught by exactly this check, and nothing subtler is claimed.
    """
    import re

    def tokens(text: str) -> set[str]:
        return set(re.findall(r"(?:₹\s?[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?%?)", text))

    invented = tokens(rewritten) - tokens(original.body)
    return tuple(sorted(invented))

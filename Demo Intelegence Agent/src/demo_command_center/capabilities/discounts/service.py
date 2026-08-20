"""Capability 034 — Discount Suggestion.

Fully deterministic. The engine reads the versioned policy, the authoritative
plan quote and the recorded objections, and returns a `DiscountDecision`. The
LLM is never called here and never sees the policy: its only role, one layer up,
is wording an offer this function has already approved.

The order of checks is the abuse model:

1. **Eligibility gates first** — a demo that never completed, a customer already
   given two offers this quarter, a repeat asker. Cheap, and they short-circuit
   before any band is selected.
2. **Band selection from *explicit* objections only.** An inferred price concern
   is a guess; paying out on it is discounting against a hallucination.
3. **Ceiling, then floor.** The band caps the percentage; the floor caps the
   resulting amount. Both are needed — a 10% band is still too much on a plan
   that has already been discounted to the margin line.
4. **Escalate rather than deny** when the policy would allow more than the
   auto-approval ceiling. Denying a case a human would have approved loses the
   sale silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from demo_command_center.config.policies import DiscountBand, DiscountPolicy
from demo_command_center.contracts.common import DemoOutcome
from demo_command_center.domain.objections import ObjectionAnalysisV1, ObjectionCategory
from demo_command_center.domain.pricing import (
    DenialReason,
    DiscountDecision,
    DiscountStatus,
    PlanQuote,
)
from demo_command_center.observability.logging import get_logger
from demo_command_center.shared.money import Money

logger = get_logger("capability.discounts")


class DiscountCapability:
    def __init__(self, policy: DiscountPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        *,
        conversation_ref: str,
        demo_id: str,
        student_ref: str | None,
        quote: PlanQuote,
        analysis: ObjectionAnalysisV1 | None,
        outcome: DemoOutcome,
        prior_offers: int,
        repeat_requests: int,
        now: datetime,
        enabled: bool = True,
    ) -> DiscountDecision:
        """The single entry point. Always returns a decision, never raises."""
        floor = quote.list_price.percent(self._policy.price_floor_percent_of_list)

        def refuse(reason: DenialReason, *, status: DiscountStatus) -> DiscountDecision:
            return self._decision(
                conversation_ref=conversation_ref,
                demo_id=demo_id,
                student_ref=student_ref,
                quote=quote,
                band=None,
                percent=0,
                floor=floor,
                status=status,
                reason=reason,
                triggers=(),
                now=now,
            )

        if not enabled:
            return refuse(DenialReason.POLICY_DISABLED, status=DiscountStatus.NOT_APPLICABLE)

        if outcome in (DemoOutcome.NOT_HELD, DemoOutcome.UNKNOWN):
            # Discounting a demo that never happened rewards no-shows.
            return refuse(DenialReason.DEMO_NOT_COMPLETED, status=DiscountStatus.DENIED)

        if prior_offers >= self._policy.max_offers_per_customer_per_days:
            return refuse(DenialReason.OFFER_LIMIT_REACHED, status=DiscountStatus.DENIED)

        if repeat_requests >= self._policy.escalate_after_repeat_requests:
            # Asking repeatedly after a decline is a negotiation tactic. It gets
            # a human, not a bigger number.
            return self._decision(
                conversation_ref=conversation_ref,
                demo_id=demo_id,
                student_ref=student_ref,
                quote=quote,
                band=None,
                percent=0,
                floor=floor,
                status=DiscountStatus.ESCALATED,
                reason=DenialReason.REPEAT_REQUESTS,
                triggers=(),
                now=now,
                requires_human=True,
            )

        triggers = analysis.categories(explicit_only=True) if analysis else frozenset()
        band = self._select_band(triggers)
        if band is None or band.max_percent == 0:
            return refuse(
                DenialReason.NO_QUALIFYING_OBJECTION, status=DiscountStatus.NOT_APPLICABLE
            )

        percent = band.max_percent
        payable = quote.list_price - quote.list_price.percent(percent)

        if payable < floor:
            # Try the band's minimum before giving up; the floor may permit the
            # smaller end of the same band.
            percent = band.min_percent
            payable = quote.list_price - quote.list_price.percent(percent)
            if payable < floor or percent == 0:
                logger.info(
                    "discount refused by price floor",
                    extra={"dcc_band": band.name, "dcc_reason": "price_floor"},
                )
                return refuse(DenialReason.PRICE_FLOOR_BREACHED, status=DiscountStatus.DENIED)

        needs_human = percent > self._policy.max_auto_approve_percent
        return self._decision(
            conversation_ref=conversation_ref,
            demo_id=demo_id,
            student_ref=student_ref,
            quote=quote,
            band=band,
            percent=percent,
            floor=floor,
            status=DiscountStatus.ESCALATED if needs_human else DiscountStatus.APPROVED,
            reason=DenialReason.NONE,
            triggers=tuple(
                sorted(triggers & set(_as_categories(band.triggers)), key=lambda c: c.value)
            ),
            now=now,
            requires_human=needs_human,
        )

    def approve_escalated(
        self, decision: DiscountDecision, *, approver: str, now: datetime
    ) -> DiscountDecision:
        """A human signs off. The percentage is not re-negotiable here.

        An operator may approve or not approve what the engine computed; they
        cannot type a different number into this path, because the ceiling and
        floor checks already ran against the value in the decision.
        """
        if decision.status is not DiscountStatus.ESCALATED:
            raise ValueError(f"cannot approve a {decision.status.value} decision")
        if not approver.strip():
            raise ValueError("approval requires an identified approver")
        if decision.percent > self._policy.absolute_max_percent:
            raise ValueError("decision exceeds the absolute policy ceiling")
        return decision.model_copy(
            update={
                "status": DiscountStatus.APPROVED,
                "approved_by": approver[:64],
                "decided_at": now,
                "valid_until": now + timedelta(hours=self._policy.validity_hours),
            }
        )

    # ------------------------------------------------------------- internals
    def _select_band(self, triggers: frozenset[ObjectionCategory]) -> DiscountBand | None:
        """Highest-paying band whose triggers are **all** satisfied.

        Every declared trigger must be present, not merely one of them. That
        distinction is worth real money: `retention_recovery` declares
        `[price_concern, competitor_mention]` and its conditions say "explicit
        price objection AND a named alternative provider". Matching on *any*
        trigger handed the 12-15% band to every ordinary price-sensitive
        customer — a 5% overpay on each one, and an escalation to a human for a
        case that should have auto-approved at 10%.

        A band with no triggers declared never matches, which keeps the `none`
        band from being selected for everybody.
        """
        if not triggers:
            return None
        applicable = [
            band
            for band in self._policy.bands
            if band.triggers and set(band.triggers) <= {c.value for c in triggers}
        ]
        if not applicable:
            return None
        return max(applicable, key=lambda b: (b.max_percent, b.min_percent))

    def _decision(
        self,
        *,
        conversation_ref: str,
        demo_id: str,
        student_ref: str | None,
        quote: PlanQuote,
        band: DiscountBand | None,
        percent: int,
        floor: Money,
        status: DiscountStatus,
        reason: DenialReason,
        triggers: tuple[ObjectionCategory, ...],
        now: datetime,
        requires_human: bool = False,
    ) -> DiscountDecision:
        discount = quote.list_price.percent(percent) if percent else Money(0, quote.currency)
        payable = quote.list_price - discount
        return DiscountDecision(
            conversation_ref=conversation_ref,
            demo_id=demo_id,
            student_ref=student_ref,
            status=status,
            band_name=band.name if band else "",
            percent=percent,
            list_price_minor=quote.list_price_minor,
            discount_minor=discount.minor,
            payable_minor=payable.minor,
            floor_minor=floor.minor,
            currency=quote.currency,
            triggers=triggers,
            conditions=tuple(band.conditions) if band else (),
            reason_code=reason,
            requires_human_approval=requires_human,
            policy_stamp=self._policy.stamp,
            valid_until=now + timedelta(hours=self._policy.validity_hours) if percent else None,
            decided_at=now,
        )


def _as_categories(values: tuple[str, ...]) -> list[ObjectionCategory]:
    """Policy trigger strings → enum, ignoring anything unrecognised.

    Ignoring rather than raising: a policy naming a category this build does not
    know about should narrow the match, not take the discount engine down.
    """
    out: list[ObjectionCategory] = []
    for value in values:
        try:
            out.append(ObjectionCategory(value))
        except ValueError:
            logger.warning("discount policy names an unknown objection category")
    return out

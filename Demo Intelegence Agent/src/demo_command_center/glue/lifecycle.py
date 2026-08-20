"""The full demo lifecycle, driven through the real orchestrator.

Shared by `make demo` (the CLI) and `tests/e2e/`. Deliberately *not* a script
that calls capabilities directly: every step here fires a `Trigger` at
`DemoCommandCenterOrchestrator.handle()`, so the state machine, the ownership
checks, the guards, the idempotency claims and the single outbound boundary are
all exercised. A harness that bypassed them would prove nothing about the system
that actually runs.

Each step returns a `Step` record with the resulting state, so a failure is
reported as "step 7 expected SLOT_HELD, got NEGOTIATING_SLOT" rather than as a
stack trace three layers down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from demo_command_center.contracts.common import DemoMode, Language, Party, Requirement
from demo_command_center.domain.demo import AttendanceSignal, DemoRequest
from demo_command_center.domain.objections import ObjectionCategory
from demo_command_center.domain.payments import PaymentEvent, PaymentEventKind
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.orchestration.context import Dependencies
from demo_command_center.orchestration.orchestrator import (
    DemoCommandCenterOrchestrator,
    TurnResult,
)
from demo_command_center.shared.ids import prefixed
from demo_command_center.state.states import DemoState
from demo_command_center.state.triggers import Actor, Trigger


@dataclass(slots=True)
class Step:
    index: int
    name: str
    trigger: str
    state: str
    ok: bool
    detail: str = ""
    messages_sent: int = 0

    def render(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        extra = f"  ({self.detail})" if self.detail else ""
        return f"  [{mark}] {self.index:2d}. {self.name:<34} -> {self.state}{extra}"


@dataclass(slots=True)
class LifecycleReport:
    conversation_ref: str
    steps: list[Step] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def final_state(self) -> str:
        return self.steps[-1].state if self.steps else DemoState.NEW.value

    def render(self) -> str:
        lines = [f"Demo Command Center — full lifecycle ({self.conversation_ref})", ""]
        lines.extend(step.render() for step in self.steps)
        lines.append("")
        lines.append(f"  {sum(1 for s in self.steps if s.ok)}/{len(self.steps)} steps passed")
        lines.append(f"  final state: {self.final_state}")
        lines.append(f"  messages delivered: {len(self.messages)}")
        return "\n".join(lines)


class LifecycleRunner:
    """Drives one conversation from handoff to converted."""

    def __init__(
        self,
        orchestrator: DemoCommandCenterOrchestrator,
        deps: Dependencies,
        doubles: dict[str, Any],
        *,
        conversation_ref: str = "cv_lifecycle_demo",
    ) -> None:
        self._o = orchestrator
        self._d = deps
        self._doubles = doubles
        self._ref = conversation_ref
        self._report = LifecycleReport(conversation_ref=conversation_ref)
        self._counter = 0

    async def run(self) -> LifecycleReport:
        await self._seed_request()

        # --- 1-4: handoff, ownership, identity, requirements
        await self._fire(
            "Lead Intake handoff",
            Trigger.HANDOFF_RECEIVED,
            Actor.AGENT,
            expect=DemoState.OWNERSHIP_ACQUIRING,
        )
        await self._fire(
            "ownership acquired",
            Trigger.OWNERSHIP_ACQUIRED,
            Actor.SYSTEM,
            expect=DemoState.IDENTITY_RESOLUTION,
        )
        await self._fire(
            "identity resolved",
            Trigger.IDENTITY_RESOLVED,
            Actor.SYSTEM,
            expect=DemoState.COLLECTING_REQUIREMENTS,
            payload={"phone_hash": "ph_fixture_0001"},
        )
        await self._fire(
            "requirements complete",
            Trigger.REQUIREMENTS_COMPLETE,
            Actor.SYSTEM,
            expect=DemoState.TUTOR_MATCH_REQUESTED,
        )

        # --- 5-7: tutor discovery through Tutor Intelligence (return-only)
        await self._fire(
            "tutor match returned",
            Trigger.MATCH_SUCCEEDED,
            Actor.SYSTEM,
            expect=DemoState.TUTOR_OPTIONS_READY,
        )
        await self._fire(
            "options presented",
            Trigger.OPTIONS_PRESENTED,
            Actor.SYSTEM,
            expect=DemoState.AWAITING_TUTOR_SELECTION,
        )

        candidates = await self._d.demos.load_candidates(self._ref)
        chosen = candidates[0] if candidates else None
        # The parent taps the first button. The orchestrator resolves it against
        # the stored snapshot — the harness never supplies a tutor reference.
        await self._fire(
            "tutor selected",
            Trigger.TUTOR_CHOSEN,
            Actor.USER,
            expect=DemoState.TUTOR_SELECTED,
            payload={"button_id": "tutor:1", "text": "1"},
        )

        # --- 8-10: slot negotiation and the atomic hold
        slot = await self._first_slot(chosen.tutor_ref if chosen else "")
        await self._fire(
            "slot proposed", Trigger.SLOT_PROPOSED, Actor.SYSTEM, expect=DemoState.NEGOTIATING_SLOT
        )
        # Only the slot is supplied. The tutor comes from the validated
        # selection fact — a payload `tutor_ref` is deliberately ignored.
        await self._fire(
            "slot agreed",
            Trigger.SLOT_AGREED,
            Actor.USER,
            expect=DemoState.SLOT_HELD,
            payload={"slot": slot},
        )
        await self._fire(
            "tutor confirmation sent",
            Trigger.HOLD_PLACED,
            Actor.SYSTEM,
            expect=DemoState.TUTOR_CONFIRMATION_PENDING,
        )

        # --- 11-13: tutor accepts, calendar + Meet, confirmation
        await self._fire(
            "tutor accepted",
            Trigger.TUTOR_ACCEPTED,
            Actor.TUTOR,
            expect=DemoState.CALENDAR_CREATION_PENDING,
        )
        await self._fire(
            "calendar event + Meet created",
            Trigger.CALENDAR_CREATED,
            Actor.SYSTEM,
            expect=DemoState.SCHEDULED,
        )
        await self._fire(
            "reminders scheduled",
            Trigger.REMINDERS_SCHEDULED,
            Actor.SYSTEM,
            expect=DemoState.REMINDERS_ACTIVE,
        )

        # --- 14: reschedule, proving the ladder is replaced not appended
        await self._reschedule()

        # --- 15-16: the demo runs, outcome captured from the calendar
        await self._fire(
            "demo window open",
            Trigger.DEMO_WINDOW_OPEN,
            Actor.SCHEDULER,
            expect=DemoState.DEMO_READY,
        )
        await self._mark_attendance()
        await self._fire(
            "outcome due", Trigger.OUTCOME_DUE, Actor.SCHEDULER, expect=DemoState.OUTCOME_PENDING
        )
        await self._fire(
            "demo completed",
            Trigger.DEMO_COMPLETED,
            Actor.SYSTEM,
            expect=DemoState.COMPLETED,
            payload={"transcript": _TRANSCRIPT},
        )

        # --- 17-18: objections + deterministic forecast, then follow-up
        await self._fire(
            "post-demo analysis",
            Trigger.ANALYSIS_REQUESTED,
            Actor.SYSTEM,
            expect=DemoState.POST_DEMO_ANALYSIS,
            payload={"transcript": _TRANSCRIPT},
        )
        await self._decide_discount()
        await self._fire(
            "follow-up ready",
            Trigger.ANALYSIS_COMPLETE,
            Actor.SYSTEM,
            expect=DemoState.FOLLOWUP_PENDING,
        )

        # --- 19-22: payment, verified webhook, activation, onboarding
        await self._fire(
            "payment link issued",
            Trigger.PAYMENT_LINK_ISSUED,
            Actor.SYSTEM,
            expect=DemoState.PAYMENT_PENDING,
        )
        await self._pay()
        await self._fire(
            "activation started",
            Trigger.ACTIVATION_STARTED,
            Actor.SYSTEM,
            expect=DemoState.SUBSCRIPTION_ACTIVATING,
        )
        await self._fire(
            "subscription activated",
            Trigger.ACTIVATION_SUCCEEDED,
            Actor.SYSTEM,
            expect=DemoState.ONBOARDING_HANDOFF_PENDING,
        )
        await self._fire(
            "onboarding accepted",
            Trigger.ONBOARDING_ACCEPTED,
            Actor.AGENT,
            expect=DemoState.CONVERTED,
        )

        self._report.messages = list(self._doubles["whatsapp"].bodies())
        return self._report

    # ------------------------------------------------------------ sub-steps
    async def _seed_request(self) -> None:
        """The demo request Lead Intake's handoff would have carried."""
        await self._d.demos.save_request(
            DemoRequest(
                request_id=prefixed("req", now=self._d.clock.now()),
                conversation_ref=self._ref,
                student_ref="stu_fixture_0001",
                requirement=Requirement(
                    service="board_exam_prep",
                    board="CBSE",
                    student_class="10",
                    subject="Mathematics",
                    mode=DemoMode.ONLINE,
                    timezone="Asia/Kolkata",
                ),
                language=Language.EN,
                region="north",
                created_at=self._d.clock.now(),
            )
        )
        await self._d.conversations.touch_inbound(self._ref, at=self._d.clock.now())

    async def _first_slot(self, tutor_ref: str) -> TimeSlot:
        now = self._d.clock.now()
        slots = await self._d.gateway.tutor_availability(
            tutor_ref=tutor_ref, from_at=now + timedelta(hours=2), to_at=now + timedelta(days=7)
        )
        return slots[0] if slots else TimeSlot(starts_at=now + timedelta(days=1))

    async def _reschedule(self) -> None:
        demo = await self._d.demos.for_conversation(self._ref)
        before = len(self._d.reminders.all_rows()) if hasattr(self._d.reminders, "all_rows") else 0
        result = await self._turn(Trigger.RESCHEDULE_REQUESTED, Actor.USER, payload={})
        pending = (
            sum(1 for r in self._d.reminders.all_rows() if r.status.value == "pending")
            if hasattr(self._d.reminders, "all_rows")
            else 0
        )
        self._record(
            "reschedule requested",
            Trigger.RESCHEDULE_REQUESTED,
            result,
            ok=result.state == DemoState.NEGOTIATING_SLOT.value,
            detail=f"reminders {before} -> {pending} pending",
        )
        # Re-book onto a new slot so the lifecycle continues.
        if demo is not None:
            slot = await self._first_slot(demo.tutor_ref or "")
            moved = TimeSlot(
                starts_at=slot.starts_at + timedelta(days=1),
                duration_minutes=slot.duration_minutes,
                timezone=slot.timezone,
            )
            await self._fire(
                "re-agreed slot",
                Trigger.SLOT_AGREED,
                Actor.USER,
                expect=DemoState.SLOT_HELD,
                payload={"slot": moved},
            )
            await self._fire(
                "re-confirmation sent",
                Trigger.HOLD_PLACED,
                Actor.SYSTEM,
                expect=DemoState.TUTOR_CONFIRMATION_PENDING,
            )
            await self._fire(
                "tutor re-accepted",
                Trigger.TUTOR_ACCEPTED,
                Actor.TUTOR,
                expect=DemoState.CALENDAR_CREATION_PENDING,
            )
            await self._fire(
                "calendar updated",
                Trigger.CALENDAR_CREATED,
                Actor.SYSTEM,
                expect=DemoState.SCHEDULED,
            )
            await self._fire(
                "reminders replaced",
                Trigger.REMINDERS_SCHEDULED,
                Actor.SYSTEM,
                expect=DemoState.REMINDERS_ACTIVE,
            )

    async def _mark_attendance(self) -> None:
        """Both parties joined, observed by the conference — not by a model."""
        demo = await self._d.demos.for_conversation(self._ref)
        calendar = self._doubles.get("calendar")
        mark = getattr(calendar, "mark_attendance", None)
        if demo is not None and demo.calendar_event_id and callable(mark):
            mark(demo.calendar_event_id, participants=2, duration_minutes=44)

    async def _decide_discount(self) -> None:
        """The deterministic engine decides; the model never sees the numbers."""
        demo = await self._d.demos.for_conversation(self._ref)
        if demo is None:
            return
        quote = await self._d.gateway.plan_quote(student_ref=demo.student_ref, plan_ref=None)
        analysis = await self._d.analysis.load_objections(demo.demo_id)
        decision = self._d.discounts.evaluate(
            conversation_ref=self._ref,
            demo_id=demo.demo_id,
            student_ref=demo.student_ref,
            quote=quote,
            analysis=analysis,
            outcome=demo.outcome.outcome,
            prior_offers=0,
            repeat_requests=0,
            now=self._d.clock.now(),
        )
        if decision.status.value == "escalated":
            decision = self._d.discounts.approve_escalated(
                decision, approver="ops_fixture", now=self._d.clock.now()
            )
        await self._d.commerce.save_decision(decision)
        self._counter += 1
        self._report.steps.append(
            Step(
                index=self._counter,
                name="discount decided (deterministic)",
                trigger="-",
                state=(await self._o.snapshot(self._ref)).state.value,
                ok=decision.payable_minor >= decision.floor_minor,
                detail=(
                    f"{decision.status.value} {decision.percent}% "
                    f"band={decision.band_name or 'none'}"
                ),
            )
        )

    async def _pay(self) -> None:
        """A verified server-to-server event — never a customer's claim."""
        order = await self._d.commerce.order_for_conversation(self._ref)
        if order is None:
            self._record(
                "payment verified",
                Trigger.PAYMENT_PAID,
                None,
                ok=False,
                detail="no order was created",
            )
            return

        event = PaymentEvent(
            provider_event_id=f"cf_evt_{order.order_ref[-8:]}",
            kind=PaymentEventKind.SUCCESS,
            order_ref=order.order_ref,
            amount_minor=order.amount_minor,
            currency=order.currency,
            occurred_at=self._d.clock.now(),
            signature_verified=True,
        )
        webhook = await self._d.paid.accept_webhook(event)
        result = await self._turn(
            Trigger.PAYMENT_PAID,
            Actor.PAYMENT_PROVIDER,
            payload={
                "signature_verified": True,
                "amount_matches_order": webhook.accepted,
                "order_belongs_to_conversation": webhook.accepted,
                "order_ref": order.order_ref,
            },
        )
        self._record(
            "payment verified",
            Trigger.PAYMENT_PAID,
            result,
            ok=result.state == DemoState.PAYMENT_CONFIRMED.value,
            detail=f"order {order.order_ref[-8:]} {order.amount.display()}",
        )

    # ------------------------------------------------------------- plumbing
    async def _fire(
        self,
        name: str,
        trigger: Trigger,
        actor: Actor,
        *,
        expect: DemoState,
        payload: dict[str, Any] | None = None,
    ) -> TurnResult:
        result = await self._turn(trigger, actor, payload=payload or {})
        self._record(
            name,
            trigger,
            result,
            ok=result.state == expect.value,
            detail="" if result.state == expect.value else f"expected {expect.value}",
        )
        return result

    async def _turn(
        self, trigger: Trigger, actor: Actor, *, payload: dict[str, Any] | None = None
    ) -> TurnResult:
        self._counter += 1
        return await self._o.handle(
            conversation_ref=self._ref,
            trigger=trigger,
            actor=actor,
            # Unique per step: the orchestrator dedupes on it, so a repeated
            # trigger inside one run must carry a distinct event id.
            event_id=f"evt_{self._counter:03d}",
            payload=payload or {},
            trace_id="tr_lifecycle",
        )

    def _record(
        self,
        name: str,
        trigger: Trigger | None,
        result: TurnResult | None,
        *,
        ok: bool,
        detail: str = "",
    ) -> None:
        self._report.steps.append(
            Step(
                index=len(self._report.steps) + 1,
                name=name,
                trigger=trigger.value if trigger else "-",
                state=result.state if result else "?",
                ok=ok,
                detail=detail or (result.rejected_reason if result else ""),
                messages_sent=result.messages_sent if result else 0,
            )
        )


_TRANSCRIPT = (
    "Parent: The class was good, my daughter liked the teaching style.\n"
    "Parent: But the fees look a bit expensive for us right now.\n"
    "Parent: Let me discuss with my husband and get back to you."
)

#: What the transcript should yield. Asserted by the E2E test so a regression in
#: extraction is caught as a lifecycle failure, not just a unit-test failure.
EXPECTED_OBJECTIONS: frozenset[ObjectionCategory] = frozenset(
    {ObjectionCategory.PRICE, ObjectionCategory.DECISION_MAKER}
)

#: The attendance evidence the calendar fake reports. Anything weaker is refused
#: by the state machine's `_outcome_is_evidenced` guard.
EXPECTED_EVIDENCE = AttendanceSignal.MEET_PARTICIPATION

__all__ = [
    "EXPECTED_EVIDENCE",
    "EXPECTED_OBJECTIONS",
    "LifecycleReport",
    "LifecycleRunner",
    "Party",
    "Step",
]

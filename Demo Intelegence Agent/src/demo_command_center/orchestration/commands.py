"""Command execution — the bridge from a transition to a capability.

One method per `Command`, dispatched through a table. Each method does the work
and **queues** any resulting message on `ctx.outbox`; none of them sends. That
is the invariant the whole design rests on, and it is enforced structurally: a
`WhatsAppPort` is not reachable from here.

Every method is written to be safe to run twice. The orchestrator persists state
before executing, so a crash mid-command is resumed by re-running it — which is
only correct because holds are keyed, orders are reused, calendar events are
requested with a stable request id and messages carry idempotency keys.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from demo_command_center.capabilities.conversion.service import ConversionFacts
from demo_command_center.contracts.common import DemoMode, DemoOutcome, Language, Party
from demo_command_center.contracts.events import DomainEvent
from demo_command_center.contracts.ownership import Owner
from demo_command_center.contracts.ports import ProviderError
from demo_command_center.contracts.tutor_match import TutorMatchRequestV1
from demo_command_center.domain.demo import Demo, DemoAttendee
from demo_command_center.domain.messages import Button, MessageKind, OutboundMessage
from demo_command_center.domain.objections import ObjectionCategory
from demo_command_center.domain.slots import TimeSlot
from demo_command_center.integrations.meta_whatsapp.templates import (
    TEMPLATE_TUTOR_REQUEST_EXPIRED,
    TemplateNotApproved,
    registry,
)
from demo_command_center.observability import metrics
from demo_command_center.observability.logging import get_logger
from demo_command_center.orchestration import composer
from demo_command_center.orchestration.context import Dependencies, TurnContext
from demo_command_center.security.signatures import idempotency_key
from demo_command_center.shared.ids import demo_id as new_demo_id
from demo_command_center.state.transitions import Command
from demo_command_center.state.triggers import Trigger

logger = get_logger("commands")


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    failed: bool = False
    failure_reason: str = ""
    #: Only populated under `caller_sends` ownership. Normally None.
    reply_text: str | None = None
    data: dict[str, Any] | None = None
    #: Ownership to hand to, applied by the orchestrator *after* the outbox is
    #: flushed. A command that transferred ownership itself would silently
    #: suppress the very message it just queued — the outbound boundary checks
    #: ownership at send time, and by then we would no longer be the owner.
    transfer_to: Owner | None = None


class CommandExecutor:
    def __init__(self, deps: Dependencies) -> None:
        self._d = deps
        self._table: dict[
            Command, Callable[[TurnContext, dict[str, Any]], Awaitable[CommandOutcome]]
        ] = {
            Command.RESOLVE_IDENTITY: self._resolve_identity,
            Command.ASK_MISSING_REQUIREMENTS: self._ask_requirements,
            Command.REQUEST_TUTOR_MATCH: self._request_match,
            Command.PRESENT_TUTOR_OPTIONS: self._present_options,
            Command.PROPOSE_SLOTS: self._propose_slots,
            Command.PLACE_SLOT_HOLD: self._place_hold,
            Command.RELEASE_SLOT_HOLD: self._release_hold,
            Command.REQUEST_TUTOR_CONFIRMATION: self._request_tutor_confirmation,
            Command.TRY_FALLBACK_TUTOR: self._try_fallback,
            Command.CREATE_CALENDAR_EVENT: self._create_event,
            Command.CANCEL_CALENDAR_EVENT: self._cancel_event,
            Command.SEND_CONFIRMATION: self._send_confirmation,
            Command.SCHEDULE_REMINDERS: self._schedule_reminders,
            Command.CANCEL_REMINDERS: self._cancel_reminders,
            Command.CAPTURE_OUTCOME: self._capture_outcome,
            Command.RUN_POST_DEMO_ANALYSIS: self._post_demo_analysis,
            Command.SEND_FOLLOWUP: self._send_followup,
            Command.CREATE_PAYMENT_ORDER: self._create_payment_order,
            Command.ACTIVATE_SUBSCRIPTION: self._activate,
            Command.HANDOFF_TO_ONBOARDING: self._handoff_onboarding,
            Command.SEND_WELCOME: self._send_welcome,
            Command.RAISE_HUMAN_CASE: self._raise_human_case,
            Command.NOTIFY_CANCELLATION: self._notify_cancellation,
        }

    async def execute(
        self, command: Command, ctx: TurnContext, *, payload: dict[str, Any]
    ) -> CommandOutcome:
        handler = self._table.get(command)
        if handler is None:
            return CommandOutcome()
        return await handler(ctx, payload)

    # ------------------------------------------------------------- intake
    async def _resolve_identity(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        phone_hash = str(payload.get("phone_hash") or "")
        try:
            identity = await self._d.gateway.resolve_identity(
                phone_hash=phone_hash, conversation_ref=ctx.conversation_ref
            )
        except ProviderError as exc:
            # Not fatal: a parent without a website account can still book.
            ctx.degraded.append(f"identity:{exc.code or 'unavailable'}")
            return CommandOutcome(data={"student_ref": None})
        ctx.fact("student_ref", identity.get("student_ref"))
        return CommandOutcome(data=identity)

    async def _ask_requirements(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        request = await self._d.demos.request_for_conversation(ctx.conversation_ref)
        missing = request.requirement.missing() if request else ("service",)
        if not missing:
            return CommandOutcome()
        text = composer.ask_for(missing[0], language=request.language if request else None)
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.QUESTION,
                recipient_ref=ctx.conversation_ref,
                body=text,
                key_parts=("ask", missing[0], str(len(missing))),
            )
        )
        return CommandOutcome(reply_text=text)

    # ---------------------------------------------------- tutor discovery
    async def _request_match(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        """Call Tutor Intelligence in return-only mode and store the snapshot."""
        request = await self._d.demos.request_for_conversation(ctx.conversation_ref)
        if request is None or not request.requirement.complete:
            return CommandOutcome(failed=True, failure_reason="requirements_incomplete")

        requirement = request.requirement
        already_seen = tuple(
            c.tutor_ref for c in await self._d.demos.load_candidates(ctx.conversation_ref)
        )
        match_request = TutorMatchRequestV1(
            trace_id=ctx.trace_id,
            correlation_id=ctx.correlation_id,
            causation_id=ctx.causation_id,
            conversation_ref=ctx.conversation_ref,
            student_ref=request.student_ref,
            subject=requirement.subject or "",
            student_class=requirement.student_class or "",
            board=requirement.board or "",
            mode=requirement.mode or DemoMode.ONLINE,
            region=requirement.region,
            locality=requirement.locality,
            timezone=requirement.timezone,
            language=requirement.language.value,
            exclude_tutor_refs=already_seen if payload.get("exclude_seen") else (),
        )

        try:
            result = await self._d.tutors.match_tutors(match_request)
        except ProviderError as exc:
            ctx.degraded.append(f"tutor_match:{exc.code or 'unavailable'}")
            return CommandOutcome(failed=True, failure_reason="tutor_match_unavailable")

        problems = result.validate_for_presentation(now=ctx.now)
        if problems:
            logger.info("tutor result not presentable", extra={"dcc_problems": ",".join(problems)})
            ctx.fact("has_candidates", False)
            return CommandOutcome(data={"problems": list(problems)})

        presentable = result.presentable(now=ctx.now)
        await self._d.demos.save_candidates(
            conversation_ref=ctx.conversation_ref,
            match_session_id=result.match_session_id,
            candidates=presentable,
            captured_at=ctx.now,
        )
        ctx.merge(has_candidates=bool(presentable), match_session_id=result.match_session_id)
        return CommandOutcome(data={"candidates": len(presentable)})

    async def _present_options(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        candidates = await self._d.demos.load_candidates(ctx.conversation_ref)
        if not candidates:
            return CommandOutcome(failed=True, failure_reason="no_candidates")
        text = composer.tutor_options(candidates)
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.TUTOR_OPTIONS,
                recipient_ref=ctx.conversation_ref,
                body=text,
                buttons=tuple(
                    Button(reply_id=f"tutor:{c.rank}", title=_button_title(c.name, c.rank))
                    for c in candidates
                ),
                key_parts=("options", ctx.facts.get("match_session_id", "")),
            )
        )
        return CommandOutcome(reply_text=text)

    # --------------------------------------------------------- scheduling
    async def _propose_slots(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        if not self._d.scheduling_enabled:
            return CommandOutcome(failed=True, failure_reason="scheduling_disabled")
        # Facts only. A `tutor_ref` in the payload is caller-supplied and would
        # bypass the snapshot check that `_resolve_selection` performs.
        tutor_ref = str(ctx.facts.get("tutor_ref") or "")
        if not tutor_ref:
            return CommandOutcome(failed=True, failure_reason="no_tutor_selected")

        preferred = payload.get("preferred_slot")
        request = await self._d.demos.request_for_conversation(ctx.conversation_ref)
        timezone = request.requirement.timezone if request else "Asia/Kolkata"

        proposals = await self._d.scheduling.propose(
            tutor_ref=tutor_ref,
            preferred=preferred if isinstance(preferred, TimeSlot) else None,
            timezone=timezone,
        )
        ctx.degraded.extend(proposals.degraded)
        if proposals.empty:
            return CommandOutcome(failed=True, failure_reason=proposals.reason or "no_slots")

        text = composer.slot_options(proposals.proposals)
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.SLOT_PROPOSAL,
                recipient_ref=ctx.conversation_ref,
                body=text,
                buttons=tuple(
                    Button(reply_id=f"slot:{p.rank}", title=f"Option {p.rank}")
                    for p in proposals.proposals
                ),
                key_parts=("slots", tutor_ref, str(len(proposals.proposals))),
            )
        )
        return CommandOutcome(reply_text=text, data={"proposals": len(proposals.proposals)})

    async def _place_hold(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        slot = payload.get("slot")
        # The slot is legitimately caller-supplied (the parent picked a time);
        # the tutor is not — it comes from the validated selection fact.
        tutor_ref = str(ctx.facts.get("tutor_ref") or "")
        if not isinstance(slot, TimeSlot) or not tutor_ref:
            return CommandOutcome(failed=True, failure_reason="slot_or_tutor_missing")
        if not ctx.facts.get("tutor_in_snapshot"):
            return CommandOutcome(failed=True, failure_reason="tutor_not_in_presented_candidates")

        request = await self._d.demos.request_for_conversation(ctx.conversation_ref)
        mode = (request.requirement.mode if request else None) or DemoMode.ONLINE
        hold = await self._d.scheduling.hold(
            conversation_ref=ctx.conversation_ref, tutor_ref=tutor_ref, slot=slot, mode=mode
        )
        if hold is None:
            metrics.emit(metrics.Metric.SLOT_HOLD_CONFLICT)
            return CommandOutcome(failed=True, failure_reason="slot_taken")

        metrics.emit(metrics.Metric.SLOT_HOLD_CREATED)
        await self._ensure_demo(ctx, tutor_ref=tutor_ref, mode=mode)
        ctx.merge(hold_id=hold.hold_id, hold_expired=False)
        await self._emit(ctx, DomainEvent.SLOT_HELD, {"hold_id": hold.hold_id})
        return CommandOutcome(data={"hold_id": hold.hold_id})

    async def _release_hold(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        hold_ref = str(ctx.facts.get("hold_id") or "")
        if hold_ref:
            await self._d.scheduling.release(hold_ref)
            ctx.merge(hold_id=None)
        return CommandOutcome()

    async def _request_tutor_confirmation(
        self, ctx: TurnContext, payload: dict[str, Any]
    ) -> CommandOutcome:
        """Ask the tutor to accept. Template-gated — see the template registry.

        When the approved template name is unknown, this degrades rather than
        fails: the confirmation is skipped and the demo proceeds on the
        operator's implicit approval path, which is recorded.
        """
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        hold_ref = str(ctx.facts.get("hold_id") or "")
        if demo is None or not hold_ref:
            return CommandOutcome(failed=True, failure_reason="no_demo_or_hold")

        binding = composer.tutor_confirmation_template(demo)
        if binding is None:
            ctx.degraded.append("tutor_confirmation_template_unavailable")
            logger.warning("tutor confirmation template not approved; skipping request")
            return CommandOutcome(data={"skipped": True})

        tutor = demo.attendee(Party.TUTOR)
        if tutor is None:
            return CommandOutcome(failed=True, failure_reason="no_tutor_contact")

        ctx.outbox.append(
            OutboundMessage(
                conversation_ref=ctx.conversation_ref,
                recipient_ref=tutor.ref,
                audience=Party.TUTOR,
                kind=MessageKind.TUTOR_REQUEST,
                language=demo.language,
                template=binding,
                idempotency_key=idempotency_key("tutor_req", demo.demo_id, str(demo.revision)),
                demo_id=demo.demo_id,
                trace_id=ctx.trace_id,
                created_at=ctx.now,
            )
        )
        # Expiry is a scheduled trigger, not a timer held in memory.
        await self._d.scheduler.schedule(
            name=f"tutor-expiry-{demo.demo_id}-{demo.revision}",
            fire_at=ctx.now + timedelta(minutes=20),
            payload={
                "conversation_ref": ctx.conversation_ref,
                "trigger": "tutor_request_expired",
                "demo_id": demo.demo_id,
            },
        )
        return CommandOutcome()

    async def _try_fallback(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        """After a decline or expiry: re-match, excluding who we already tried."""
        await self._notify_request_expired(ctx, payload)
        await self._release_hold(ctx, payload)
        return await self._request_match(ctx, {"exclude_seen": True})

    async def _notify_request_expired(self, ctx: TurnContext, payload: dict[str, Any]) -> None:
        """Tell the tutor their booking request lapsed.

        Only on expiry, never on a decline: a tutor who declined already knows,
        and telling them again is noise on a channel we are rate-limited on.

        Without this the tutor is left holding a request that quietly stopped
        being real — the slot is released and someone else is matched, while
        their last message still says "please confirm whether you are
        available". Accepting it later does nothing, and they may well keep the
        time free for a demo that was reassigned an hour ago.
        """
        # From the context, not the payload: the scheduler puts `trigger` at the
        # top level of the work item, so the nested `payload` the command
        # receives is empty and a payload lookup would silently never fire.
        if ctx.trigger is not Trigger.TUTOR_REQUEST_EXPIRED:
            return

        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is None:
            return
        tutor = demo.attendee(Party.TUTOR)
        if tutor is None:
            return

        try:
            binding = registry().bind(
                TEMPLATE_TUTOR_REQUEST_EXPIRED,
                language=demo.language.value,
                variables=(demo.demo_id,),
            )
        except TemplateNotApproved:
            ctx.degraded.append("tutor_request_expired_template_unavailable")
            return

        ctx.outbox.append(
            OutboundMessage(
                conversation_ref=ctx.conversation_ref,
                recipient_ref=tutor.ref,
                audience=Party.TUTOR,
                # TUTOR_REQUEST, not CANCELLATION: the audience is the tutor and
                # the kind drives throttling. A cancellation notice to a parent
                # is a different policy from a lapsed request to a tutor.
                kind=MessageKind.TUTOR_REQUEST,
                language=demo.language,
                template=binding,
                idempotency_key=idempotency_key("tutor_expired", demo.demo_id, str(demo.revision)),
                demo_id=demo.demo_id,
                trace_id=ctx.trace_id,
                created_at=ctx.now,
            )
        )

    # ----------------------------------------------------------- calendar
    async def _create_event(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        hold_ref = str(ctx.facts.get("hold_id") or "")
        hold = await self._d.slots.load_hold(hold_ref) if hold_ref else None
        if demo is None or hold is None:
            return CommandOutcome(failed=True, failure_reason="no_demo_or_hold")

        # A demo that already has an event is being *moved*. Patching keeps one
        # logical event per demo; creating a second one leaves the parent with
        # two invites and the first one still on their calendar.
        if demo.calendar_event_id:
            return await self._patch_event(ctx, demo=demo, hold=hold)

        tutor_email, student_email = await self._invite_emails(demo)
        booking = await self._d.scheduling.book(
            demo=demo,
            hold=hold,
            tutor_email=tutor_email,
            student_email=student_email,
            summary=composer.calendar_summary(demo),
            description=composer.calendar_description(demo),
        )
        if booking.failed or booking.demo is None:
            metrics.emit(metrics.Metric.MEET_CREATION_FAILURE, reason=booking.failure_reason[:40])
            return CommandOutcome(failed=True, failure_reason=booking.failure_reason)

        ctx.merge(
            calendar_event_id=booking.calendar_event_id,
            demo_id=booking.demo.demo_id,
            meet_url=booking.meet_url,
        )
        metrics.emit(metrics.Metric.DEMO_SCHEDULED)
        await self._emit(
            ctx,
            DomainEvent.SCHEDULED,
            {"demo_id": booking.demo.demo_id, "starts_at": booking.demo.slot.starts_at.isoformat()}
            if booking.demo.slot
            else {"demo_id": booking.demo.demo_id},
        )
        return CommandOutcome(data={"calendar_event_id": booking.calendar_event_id})

    async def _patch_event(self, ctx: TurnContext, *, demo: Demo, hold: Any) -> CommandOutcome:
        """Move the existing event. Bumps the revision, which is what makes the
        old reminder ladder identifiable as obsolete without a join."""
        result = await self._d.scheduling.reschedule(demo=demo, slot=hold.slot)
        if result.failed or result.demo is None:
            return CommandOutcome(failed=True, failure_reason=result.failure_reason)

        await self._d.slots.confirm(hold.hold_id, now=ctx.now)
        ctx.merge(
            calendar_event_id=result.calendar_event_id,
            demo_id=result.demo.demo_id,
            meet_url=result.meet_url,
        )
        metrics.emit(metrics.Metric.DEMO_RESCHEDULED)
        await self._emit(
            ctx,
            DomainEvent.RESCHEDULED,
            {"demo_id": result.demo.demo_id, "revision": result.demo.revision},
        )
        return CommandOutcome(data={"calendar_event_id": result.calendar_event_id})

    async def _cancel_event(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is not None:
            await self._d.scheduling.cancel(
                demo=demo, reason=str(payload.get("reason") or "cancelled")
            )
            metrics.emit(metrics.Metric.DEMO_CANCELLED)
        return CommandOutcome()

    async def _send_confirmation(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is None or demo.slot is None:
            return CommandOutcome(failed=True, failure_reason="no_scheduled_demo")
        text = composer.confirmation(demo)
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.CONFIRMATION,
                recipient_ref=ctx.conversation_ref,
                body=text,
                key_parts=("confirm", demo.demo_id, str(demo.revision)),
                demo_id=demo.demo_id,
                demo=demo,
            )
        )
        return CommandOutcome(reply_text=text)

    # ---------------------------------------------------------- reminders
    async def _schedule_reminders(
        self, ctx: TurnContext, payload: dict[str, Any]
    ) -> CommandOutcome:
        if not self._d.reminders_enabled:
            return CommandOutcome()
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is None:
            return CommandOutcome()
        planned = self._d.reminder_policy.plan(demo)
        await self._d.reminders.replace_for_demo(
            demo.demo_id, revision=demo.revision, reminders=planned
        )
        for reminder in planned:
            await self._d.scheduler.schedule(
                name=f"reminder-{reminder.reminder_id}",
                fire_at=reminder.fire_at,
                payload={"reminder_id": reminder.reminder_id, "demo_id": demo.demo_id},
            )
        return CommandOutcome(data={"reminders": len(planned)})

    async def _cancel_reminders(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        demo_ref = str(ctx.facts.get("demo_id") or "")
        if demo_ref:
            cancelled = await self._d.reminders.cancel_for_demo(demo_ref)
            return CommandOutcome(data={"cancelled": cancelled})
        return CommandOutcome()

    # ------------------------------------------------------------ outcome
    async def _capture_outcome(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        """Ask the calendar what happened. Never guesses, never asks a model."""
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is None or not demo.calendar_event_id:
            return CommandOutcome(data={"evidence_source": "none"})
        try:
            event = await self._d.scheduling.calendar.get_event(event_id=demo.calendar_event_id)
        except ProviderError:
            ctx.degraded.append("calendar_outcome_unavailable")
            return CommandOutcome(data={"evidence_source": "none"})

        evidence = composer.attendance_from_calendar(event)
        ctx.merge(
            evidence_source=evidence.evidence_source.value,
            grace_period_elapsed=demo.outcome_due(now=ctx.now),
        )
        await self._d.demos.save(
            demo.model_copy(update={"outcome": evidence, "updated_at": ctx.now})
        )
        return CommandOutcome(data={"evidence_source": evidence.evidence_source.value})

    async def _post_demo_analysis(
        self, ctx: TurnContext, payload: dict[str, Any]
    ) -> CommandOutcome:
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is None:
            return CommandOutcome()

        transcript = str(payload.get("transcript") or "")
        extraction = await self._d.objections.extract(
            demo_id=demo.demo_id,
            conversation_ref=ctx.conversation_ref,
            transcript=transcript,
            now=ctx.now,
        )
        await self._d.analysis.save_objections(extraction.analysis)
        metrics.emit(metrics.Metric.OBJECTIONS_EXTRACTED)

        from demo_command_center.capabilities.forecasting import features as feature_builder

        inputs = feature_builder.FeatureInputs(
            outcome=demo.outcome.outcome,
            reschedule_count=demo.revision - 1,
            demo_duration_minutes=demo.outcome.duration_minutes,
            mode=demo.mode,
            sample_size=int(payload.get("segment_sample_size") or 0),
        )
        forecast = self._d.forecasting.score(
            demo_id=demo.demo_id,
            features=feature_builder.build(inputs, analysis=extraction.analysis, demo=demo),
            sample_size=inputs.sample_size,
            now=ctx.now,
            has_explicit_objection=bool(extraction.analysis.categories(explicit_only=True)),
            # The enum member, not the string. A raw string only works because
            # `ObjectionCategory` is a StrEnum today; changing it to a plain
            # Enum would silently stop tutor-fit routing from ever firing.
            tutor_fit_concern=extraction.analysis.has(ObjectionCategory.TUTOR_FIT),
        )
        await self._d.analysis.save_forecast(forecast.as_row())
        await self._d.analysis.save_quality(
            {
                "demo_id": demo.demo_id,
                "score": feature_builder.demo_quality(inputs, analysis=extraction.analysis),
                "computed_at": ctx.now.isoformat(),
            }
        )
        metrics.emit(metrics.Metric.FORECAST_SCORED, band=forecast.risk_band.value)
        await self._emit(ctx, DomainEvent.OBJECTIONS_EXTRACTED, {"demo_id": demo.demo_id})
        return CommandOutcome(data={"probability": forecast.probability})

    async def _send_followup(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        if demo is None:
            return CommandOutcome()
        analysis = await self._d.analysis.load_objections(demo.demo_id)
        decision = await self._d.commerce.load_decision(demo.demo_id)
        offer = None
        # Only a *real* discount is mentioned. A 0% decision is a valid offer
        # for the payment path, but "0% off" in a follow-up reads as a bug.
        if decision is not None and decision.percent > 0:
            try:
                offer = decision.approve(now=ctx.now)
            except ValueError:
                offer = None

        composed = self._d.conversion.compose(
            facts=ConversionFacts(
                tutor_name=str(payload.get("tutor_name") or ""),
                subject=str(payload.get("subject") or ""),
                outcome=demo.outcome.outcome or DemoOutcome.UNKNOWN,
                offer=offer,
                language=demo.language,
            ),
            analysis=analysis,
            now=ctx.now,
        )
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.FOLLOWUP,
                recipient_ref=ctx.conversation_ref,
                body=composed.body,
                key_parts=("followup", demo.demo_id, str(demo.revision)),
                demo_id=demo.demo_id,
                demo=demo,
            )
        )
        return CommandOutcome(reply_text=composed.body)

    # --------------------------------------------------------- commercial
    async def _create_payment_order(
        self, ctx: TurnContext, payload: dict[str, Any]
    ) -> CommandOutcome:
        if not self._d.payments_enabled:
            return CommandOutcome(failed=True, failure_reason="payments_disabled")
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        decision = await self._d.commerce.load_decision(demo.demo_id) if demo else None
        if demo is None or decision is None:
            return CommandOutcome(failed=True, failure_reason="no_approved_offer")
        try:
            offer = decision.approve(now=ctx.now)
        except ValueError as exc:
            return CommandOutcome(failed=True, failure_reason=str(exc)[:120])

        result = await self._d.paid.create_order(offer)
        if result.failed or result.order is None:
            return CommandOutcome(failed=True, failure_reason=result.reason)

        text = composer.payment_link(offer, result.payment_link)
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.PAYMENT_LINK,
                recipient_ref=ctx.conversation_ref,
                body=text,
                key_parts=("pay", result.order.order_ref),
                demo_id=demo.demo_id,
            )
        )
        metrics.emit(metrics.Metric.PAYMENT_REQUESTED)
        await self._emit(ctx, DomainEvent.PAYMENT_REQUESTED, {"order_ref": result.order.order_ref})
        return CommandOutcome(reply_text=text, data={"order_ref": result.order.order_ref})

    async def _activate(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        order_ref = str(ctx.facts.get("order_ref") or payload.get("order_ref") or "")
        order = await self._d.commerce.load_order(order_ref) if order_ref else None
        if order is None:
            return CommandOutcome(failed=True, failure_reason="order_not_found")

        result = await self._d.paid.activate(order)
        if not result.activation.succeeded:
            metrics.emit(metrics.Metric.ACTIVATION_FAILURE)
            return CommandOutcome(
                failed=result.needs_human,
                failure_reason=result.activation.error_code or "activation_failed",
            )
        ctx.fact("subscription_ref", result.activation.subscription_ref)
        await self._emit(ctx, DomainEvent.SUBSCRIPTION_ACTIVATED, {"order_ref": order.order_ref})
        return CommandOutcome(data={"subscription_ref": result.activation.subscription_ref})

    async def _handoff_onboarding(
        self, ctx: TurnContext, payload: dict[str, Any]
    ) -> CommandOutcome:
        subscription_ref = str(ctx.facts.get("subscription_ref") or "")
        order_ref = str(ctx.facts.get("order_ref") or "")
        if not subscription_ref:
            return CommandOutcome(failed=True, failure_reason="no_subscription_ref")

        key = idempotency_key("onboarding", order_ref, subscription_ref)
        if not await self._d.idempotency.claim(
            key, scope="handoff", now=ctx.now, ttl_seconds=self._d.idempotency_ttl_seconds
        ):
            # Already handed off. Not an error — the retry did its job.
            return CommandOutcome(data={"duplicate": True})

        # The envelope goes out now; ownership does not move until Onboarding
        # accepts. Until then this conversation is still ours to answer.
        await self._emit(
            ctx,
            DomainEvent.ONBOARDING_REQUESTED,
            {"subscription_ref": subscription_ref, "order_ref": order_ref},
        )
        metrics.emit(metrics.Metric.HANDOFF_REQUESTED, destination="onboarding")
        return CommandOutcome(data={"handed_off": True})

    async def _send_welcome(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        """The last thing we say, then we let go.

        `transfer_to` rather than transferring here: the orchestrator applies it
        after the outbox is flushed, so the welcome is actually delivered.
        """
        text = composer.welcome()
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.WELCOME,
                recipient_ref=ctx.conversation_ref,
                body=text,
                key_parts=("welcome", str(ctx.facts.get("subscription_ref") or "")),
            )
        )
        return CommandOutcome(reply_text=text, transfer_to=Owner.ONBOARDING)

    # -------------------------------------------------------- exceptional
    async def _raise_human_case(self, ctx: TurnContext, payload: dict[str, Any]) -> CommandOutcome:
        case_id = await self._d.operations.open_human_case(
            {
                "conversation_ref": ctx.conversation_ref,
                "state": ctx.snapshot.state.value,
                "reason": str(payload.get("reason") or "escalated"),
                "demo_id": ctx.facts.get("demo_id"),
                "opened_at": ctx.now.isoformat(),
            }
        )
        metrics.emit(metrics.Metric.HUMAN_HANDOFF)
        await self._emit(ctx, DomainEvent.HUMAN_HANDOFF_RAISED, {"case_id": case_id})
        return CommandOutcome(data={"case_id": case_id})

    async def _notify_cancellation(
        self, ctx: TurnContext, payload: dict[str, Any]
    ) -> CommandOutcome:
        demo = await self._d.demos.for_conversation(ctx.conversation_ref)
        text = composer.cancellation(demo)
        ctx.outbox.append(
            self._message(
                ctx,
                kind=MessageKind.CANCELLATION,
                recipient_ref=ctx.conversation_ref,
                body=text,
                key_parts=("cancel", demo.demo_id if demo else ctx.conversation_ref),
                demo=demo,
            )
        )
        await self._emit(ctx, DomainEvent.CANCELLED, {"demo_id": demo.demo_id if demo else None})
        return CommandOutcome(reply_text=text)

    # ------------------------------------------------------------ helpers
    def _message(
        self,
        ctx: TurnContext,
        *,
        kind: MessageKind,
        recipient_ref: str,
        body: str,
        key_parts: tuple[str, ...],
        buttons: tuple[Button, ...] = (),
        demo_id: str | None = None,
        demo: Demo | None = None,
    ) -> OutboundMessage:
        """One place every customer-facing message is built.

        The template binding is attached here rather than at each call site so
        a new message kind cannot be added without it. `window_safe_template`
        returns None for kinds that need no template and for templates Meta has
        not approved yet, so this is safe to apply unconditionally.
        """
        binding = composer.window_safe_template(kind, demo)
        return OutboundMessage(
            conversation_ref=ctx.conversation_ref,
            recipient_ref=recipient_ref,
            audience=Party.STUDENT,
            kind=kind,
            body=body,
            # Buttons and a template are mutually exclusive at the Meta API:
            # an approved template carries its own quick replies.
            buttons=() if binding is not None else buttons,
            template=binding,
            idempotency_key=idempotency_key(ctx.conversation_ref, *key_parts),
            demo_id=demo_id or ctx.facts.get("demo_id"),
            trace_id=ctx.trace_id,
            created_at=ctx.now,
        )

    async def _emit(self, ctx: TurnContext, event: DomainEvent, payload: dict[str, Any]) -> None:
        await self._d.outbox.enqueue(
            event=event.value,
            payload={"conversation_ref": ctx.conversation_ref, **payload},
            idempotency_key=idempotency_key(
                "outbox", ctx.conversation_ref, event.value, str(payload)
            ),
            now=ctx.now,
        )

    async def _ensure_demo(self, ctx: TurnContext, *, tutor_ref: str, mode: DemoMode) -> Demo:
        """Create the demo row on first hold, or update the tutor on a retry."""
        existing = await self._d.demos.for_conversation(ctx.conversation_ref)
        request = await self._d.demos.request_for_conversation(ctx.conversation_ref)
        if existing is not None:
            updated = existing.model_copy(update={"tutor_ref": tutor_ref, "updated_at": ctx.now})
            await self._d.demos.save(updated)
            return updated

        demo = Demo(
            demo_id=new_demo_id(now=ctx.now),
            conversation_ref=ctx.conversation_ref,
            request_id=request.request_id if request else ctx.conversation_ref,
            student_ref=request.student_ref if request else None,
            tutor_ref=tutor_ref,
            region=request.region if request else None,
            mode=mode,
            language=request.language if request is not None else Language.EN,
            attendees=(
                DemoAttendee(party=Party.STUDENT, ref=ctx.conversation_ref),
                DemoAttendee(party=Party.TUTOR, ref=tutor_ref),
            ),
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        await self._d.demos.save(demo)
        ctx.fact("demo_id", demo.demo_id)
        return demo

    async def _invite_emails(self, demo: Demo) -> tuple[str | None, str | None]:
        """Authoritative emails from the gateway. Never from anything stored."""
        tutor_email: str | None = None
        student_email: str | None = None
        if demo.tutor_ref:
            try:
                contacts = await self._d.gateway.resolve_tutor_contacts(tutor_ref=demo.tutor_ref)
                tutor_email = contacts.get("email")
            except ProviderError:
                logger.warning("tutor contacts unavailable; inviting without the tutor email")
        student = demo.attendee(Party.STUDENT)
        if student is not None and student.invite_consent:
            contact = student.contact_for("email")
            student_email = contact.ref if contact else None
        return tutor_email, student_email


def _button_title(name: str, rank: int) -> str:
    """WhatsApp caps a button at 20 characters. Truncate the name, keep the
    ordinal — a parent needs to be able to say "the second one"."""
    prefix = f"{rank}. "
    return (prefix + name)[:20]

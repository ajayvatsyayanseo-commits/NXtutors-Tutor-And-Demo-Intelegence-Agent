"""The Lead Intake handoff endpoint's application service.

This is the harmony seam: it answers "is this ours, and what do we say back"
under a 2-second budget, without ever sending a WhatsApp message itself.

Order of decisions, and why:

1. **Flags first.** A disabled or out-of-bucket conversation is declined before
   any work happens, so a rollout at 5% costs nothing for the other 95%.
2. **Idempotency second.** A redelivered `wa_message_id` returns the *previous*
   reply rather than recomputing it — recomputing could produce a different
   shortlist for the same message.
3. **Routing third.** Not ours → decline, and Lead Intake keeps the
   conversation. This is the single decision that prevents two agents answering.
4. **Then the match**, with continuation resume if we were paused.

Shadow mode runs everything and then returns `DECLINED`: real data, no
parent-visible effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tutor_match_meta.config.flags import FeatureFlags, Mode, ShadowComparison
from tutor_match_meta.contracts.envelope import AgentId
from tutor_match_meta.contracts.handoff import (
    HandoffResponseV1,
    HandoffStatus,
    LeadIntakeHandoffV1,
    OutboundOwnership,
)
from tutor_match_meta.contracts.inbound import (
    InboundEnvelope,
    InboundKind,
    WhatsAppTurnV1,
)
from tutor_match_meta.observability.context import Timer, get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter
from tutor_match_meta.orchestration.continuation import ContinuationCodec
from tutor_match_meta.orchestration.routing import Intent, intent_from_upstream, route
from tutor_match_meta.orchestration.turn_service import TurnService
from tutor_match_meta.security.signing import idempotency_key

logger = get_logger("handoff")

#: A conversation in one of these is finished; a new message starts a new one.
_TERMINAL_STATES = frozenset({"CLOSED", "DEMO_HANDOFF", "NO_MATCH"})


@dataclass(slots=True)
class HandoffDependencies:
    turn_service: TurnService
    flags: FeatureFlags
    continuations: ContinuationCodec
    pseudonymiser: object
    outbound_ownership: OutboundOwnership = OutboundOwnership.CALLER_SENDS
    #: Business policy: does the next action need a website account?
    account_required_for_match: bool = False
    #: Operator emergency flags. MATCHING_PAUSED declines to the caller, which
    #: is invisible to the parent because Lead Intake keeps answering.
    switches: object | None = None
    metrics: MetricsEmitter | None = None


class HandoffService:
    """Handles one `LeadIntakeHandoffV1` and returns a `HandoffResponseV1`."""

    def __init__(self, deps: HandoffDependencies) -> None:
        self._d = deps

    async def handle(self, request: LeadIntakeHandoffV1, *, trace_id: str) -> HandoffResponseV1:
        metrics = self._d.metrics or MetricsEmitter(enabled=False)
        conversation_id = request.effective_conversation_id()
        conversation_ref = self._d.pseudonymiser.conversation(conversation_id)  # type: ignore[attr-defined]

        # --- 0. kill switch, before flags and before any work.
        #
        # Declining is the documented safe behaviour for MATCHING_PAUSED
        # (config/kill_switches.py): Lead Intake owns the conversation and can
        # still answer, so the parent sees an ordinary reply rather than an
        # error or a silence.
        if await self._paused():
            metrics.count(Metric.KILL_SWITCH_ACTIVE)
            return HandoffResponseV1.declined("matching_paused", trace_id=trace_id)

        # --- 1. flags
        mode = self._d.flags.mode_for(conversation_ref)
        if mode is Mode.DISABLED:
            return HandoffResponseV1.declined("tutor_match_disabled", trace_id=trace_id)

        # --- 2. routing (before any expensive work)
        #
        # "Are we mid-conversation?" has three independent sources, and using
        # only one of them was a real bug: a parent's follow-up ("actually class
        # 9 now") carries no matching keyword, so without this it was declined
        # and Lead Intake would have answered a question we had asked.
        claim = self._d.continuations.try_verify(
            request.continuation_token, conversation_ref=conversation_ref
        )
        active_session = await self._has_active_session(conversation_id)
        upstream = intent_from_upstream(request.intent)
        in_active_match = claim is not None or active_session or upstream is Intent.CONTINUATION

        decision = route(
            request.message_text,
            in_active_match=in_active_match,
            account_required=self._d.account_required_for_match,
            has_account=bool(request.lead_id),
        )

        if decision.intent is Intent.HUMAN_REQUESTED:
            metrics.count(Metric.HUMAN_HANDOFF)
            return HandoffResponseV1(
                status=HandoffStatus.HUMAN_REVIEW,
                reply_text=None,
                trace_id=trace_id,
                handoff_to=AgentId.HUMAN.value,
            )

        if not decision.owned:
            # Not ours. Silence is correct: Lead Intake answers.
            return HandoffResponseV1.declined(decision.reason, trace_id=trace_id)

        if decision.needs_handoff and decision.handoff_to is AgentId.ONBOARDING:
            # Borrow onboarding, but keep the requirement so the parent never
            # repeats it. The token is the claim check.
            token, issued = self._d.continuations.issue(
                conversation_ref=conversation_ref, reason="account_required"
            )
            logger.info("pausing match for onboarding", extra={"tmm_session": issued.session_id})
            return HandoffResponseV1(
                status=HandoffStatus.NEEDS_HANDOFF,
                reply_text=None,
                trace_id=trace_id,
                handoff_to=AgentId.ONBOARDING.value,
                continuation_token=token,
                match_session_id=issued.session_id,
            )

        # --- 3. the actual turn
        envelope = self._envelope(request, conversation_id, trace_id)
        with Timer() as timer:
            result = await self._d.turn_service.handle(envelope)

        if result.duplicate:
            # A redelivery. Say nothing rather than repeating ourselves; the
            # original reply was already delivered by the caller.
            metrics.count(Metric.DUPLICATE_EVENTS)
            return HandoffResponseV1(
                status=HandoffStatus.ACCEPTED,
                reply_text=None,
                trace_id=trace_id,
                duplicate=True,
            )

        session_id = result.outcome.decision.match_session_id if result.outcome else None

        # --- 4. shadow mode: everything computed, nothing said
        if mode is Mode.SHADOW:
            comparison = ShadowComparison(
                conversation_ref=conversation_ref,
                match_session_id=session_id or "",
                would_have_matched=result.matched,
                shortlist_tutor_ids=tuple(
                    e.tutor_id
                    for e in (result.outcome.decision.shortlist if result.outcome else ())
                ),
                policy_ref=result.outcome.policy.ref if result.outcome else "",
                weight_coverage=(
                    max((c.weight_coverage for c in result.outcome.decision.scored), default=0.0)
                    if result.outcome
                    else 0.0
                ),
                latency_ms=timer.elapsed_ms,
                fabrication_violations=len(
                    result.outcome.fabrication_violations if result.outcome else ()
                ),
            )
            for key, value in comparison.as_metric_properties().items():
                metrics.property(key, value)
            logger.info("shadow decision recorded", extra={"tmm_shadow": True})
            return HandoffResponseV1(
                status=HandoffStatus.DECLINED,
                reply_text=None,
                trace_id=trace_id,
                match_session_id=session_id,
                degraded=("shadow_mode",),
            )

        if result.outcome is not None and result.outcome.needs_human:
            return HandoffResponseV1(
                status=HandoffStatus.HUMAN_REVIEW,
                reply_text=result.reply,
                trace_id=trace_id,
                match_session_id=session_id,
                handoff_to=AgentId.HUMAN.value,
                degraded=result.degraded,
            )

        if not result.reply:
            return HandoffResponseV1(
                status=HandoffStatus.ACCEPTED,
                reply_text=None,
                trace_id=trace_id,
                match_session_id=session_id,
                degraded=result.degraded,
            )

        # Under CALLER_SENDS we hand the text back and send nothing ourselves.
        # Under TUTOR_MATCH_SENDS the outbox already holds the message, so we
        # must NOT also return it — that is the double-send this enum prevents.
        if self._d.outbound_ownership is OutboundOwnership.TUTOR_MATCH_SENDS:
            return HandoffResponseV1(
                status=HandoffStatus.ACCEPTED,
                reply_text=None,
                trace_id=trace_id,
                match_session_id=session_id,
                degraded=result.degraded,
            )

        return HandoffResponseV1(
            status=HandoffStatus.HANDLED,
            reply_text=result.reply,
            trace_id=trace_id,
            match_session_id=session_id,
            degraded=result.degraded,
        )

    async def _paused(self) -> bool:
        if self._d.switches is None:
            return False
        try:
            from tutor_match_meta.config.kill_switches import Switch

            return bool(await self._d.switches.is_paused(Switch.MATCHING_PAUSED))  # type: ignore[attr-defined]
        except Exception:
            logger.warning("kill switch read failed; continuing")
            return False

    async def _has_active_session(self, conversation_id: str) -> bool:
        """True when our FSM already owns this conversation.

        The authoritative "this turn is ours" signal: if we asked the last
        question, the answer belongs to us regardless of its wording. Terminal
        states are excluded so a closed conversation starts fresh instead of
        being captured forever.
        """
        from tutor_match_meta.state.machine import ConversationState

        try:
            snapshot = await self._d.turn_service.conversations.load(conversation_id)
        except Exception:
            logger.warning("could not load conversation state; treating as new")
            return False
        return snapshot.state is not ConversationState.NEW and (
            snapshot.state.value not in _TERMINAL_STATES
        )

    def _envelope(
        self, request: LeadIntakeHandoffV1, conversation_id: str, trace_id: str
    ) -> InboundEnvelope:
        # `wa_message_id` is the authoritative dedup key: Meta redelivers a
        # webhook with the same id, and Lead Intake forwards it unchanged.
        dedup = idempotency_key("handoff", conversation_id, request.wa_message_id)
        # The recipient is carried only when this deployment owns delivery.
        # Under `caller_sends` we deliberately drop it here, so no raw phone
        # number ever reaches the outbox, the queue, or the worker.
        reply_to = (
            request.wa_phone.strip()
            if self._d.outbound_ownership is OutboundOwnership.TUTOR_MATCH_SENDS
            else None
        )
        return InboundEnvelope(
            kind=InboundKind.WHATSAPP_TURN,
            trace_id=trace_id,
            conversation_id=conversation_id,
            dedup_key=dedup,
            received_at=datetime.now(UTC),
            source_agent=request.source or AgentId.LEAD_INTAKE.value,
            reply_to=reply_to,
            payload=WhatsAppTurnV1(
                event_id=request.wa_message_id,
                conversation_id=conversation_id,
                provider_message_id=request.wa_message_id,
                text=request.message_text,
                lead_id=request.lead_id,
            ),
        )

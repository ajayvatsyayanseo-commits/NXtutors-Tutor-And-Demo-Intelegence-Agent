"""The match worker's application service — one WhatsApp turn, end to end.

This is where the pieces meet: idempotency, memory recall, requirement merge,
the FSM, the matching pipeline, persistence, deed emission and the outbound
enqueue. The orchestrator stays pure; the messy real-world sequencing lives here.

Two invariants shape the whole flow:

* **Idempotent.** A duplicate `provider_message_id` short-circuits before any
  state change. Meta redelivers webhooks routinely, and a second shortlist for
  one message is a visible, embarrassing bug.
* **Never blocked by a degraded dependency.** Memory, RAG and the LLM are all
  optional. Each failure is recorded in `degraded_sources` and the turn
  completes with a deterministic answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tutor_match_meta.analytics import (
    AnalyticsEventName,
    AnalyticsEventV1,
    AnalyticsSink,
    class_band,
    coverage_band,
)
from tutor_match_meta.cache import Cache
from tutor_match_meta.config.kill_switches import KillSwitches, Switch
from tutor_match_meta.contracts.common import Provenance, Tracked
from tutor_match_meta.contracts.enrichment import assert_supported
from tutor_match_meta.contracts.inbound import (
    InboundEnvelope,
    LeadEventV1,
    ParentSelectionV1,
    WhatsAppTurnV1,
)
from tutor_match_meta.contracts.outbound import (
    MAX_BODY_CHARS as OUTBOUND_MAX_BODY_CHARS,
)
from tutor_match_meta.contracts.outbound import (
    OUTBOX_KIND_CALLER_REPLY as OUTBOX_CALLER_REPLY,
)
from tutor_match_meta.contracts.outbound import (
    OUTBOX_KIND_DEED,
    MemoryDeedV1,
    OutboundDeliveryV1,
)
from tutor_match_meta.contracts.outbound import (
    OUTBOX_KIND_REPLY as OUTBOX_REPLY,
)
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.contracts.scoring import MatchDecisionV1
from tutor_match_meta.integrations.chitragupta.client import DeedType, entity_scope
from tutor_match_meta.integrations.llm.provider import LLMProvider, TokenBudget
from tutor_match_meta.integrations.llm.routing import (
    GuardedProvider,
    ModelRouting,
    UsageLedger,
    routing_from_settings,
)
from tutor_match_meta.observability.context import Timer, get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter
from tutor_match_meta.orchestration import output_guard, tutor_lookup
from tutor_match_meta.orchestration.extraction import (
    RequirementExtractor,
    requirement_from_lead_event,
)
from tutor_match_meta.orchestration.missing_info import assess
from tutor_match_meta.orchestration.orchestrator import MatchOutcome, TutorMatchOrchestrator
from tutor_match_meta.repositories.ports import (
    ConversationStatePort,
    DecisionPort,
    DegradedSources,
    IdempotencyPort,
    MemoryPort,
    OutboxMessage,
    OutboxPort,
    RequirementPort,
)
from tutor_match_meta.security.pii import Pseudonymiser
from tutor_match_meta.security.rate_limit import (
    AbuseDetector,
    Enforcement,
    LayeredRateLimiter,
    LimitScope,
)
from tutor_match_meta.state.machine import (
    ConversationSnapshot,
    ConversationState,
    OptimisticLockError,
    Trigger,
    try_transition,
)

logger = get_logger("turn")

IDEMPOTENCY_TTL_SECONDS = 7 * 24 * 3600

#: Ambiguous name lookups list at most this many. More than a few is not a
#: disambiguation prompt, it is a directory dump into a WhatsApp thread.
MAX_LOOKUP_RESULTS = 3

#: Enforcement rungs that shed the turn rather than serving it. Deliberately
#: excludes SOFT_THROTTLE — the first rung is a *signal*, not a refusal, and
#: refusing on it would throttle every parent who resends an unanswered
#: message. HUMAN_REVIEW is also excluded: it queues a person, it does not drop
#: the parent's turn on the floor.
_SHEDDING_ENFORCEMENTS = frozenset({Enforcement.HARD_THROTTLE, Enforcement.COOLDOWN})


@dataclass(slots=True)
class TurnResult:
    conversation_id: str
    state: ConversationState
    reply: str | None
    duplicate: bool = False
    outcome: MatchOutcome | None = None
    degraded: tuple[str, ...] = ()
    asked_for: str | None = None
    #: Set when an abuse control shed this turn. The caller sends nothing.
    throttled: bool = False
    #: Set when an operator kill switch declined the turn.
    paused: bool = False

    @property
    def matched(self) -> bool:
        return self.outcome is not None and self.outcome.matched


#: What a throttled parent sees. Deliberately human and non-accusatory: the
#: overwhelming majority of throttled turns are a retry loop or an impatient
#: resend, not an attacker, and telling a real parent they look like an
#: attacker is a worse outcome than serving one extra message.
THROTTLE_REPLY = (
    "I'm still working through your last few messages — give me a moment and "
    "I'll come back with tutor suggestions."
)


@dataclass(slots=True)
class TurnDependencies:
    """Everything the service needs, all behind ports."""

    orchestrator: TutorMatchOrchestrator
    conversations: ConversationStatePort
    requirements: RequirementPort
    decisions: DecisionPort
    idempotency: IdempotencyPort
    outbox: OutboxPort
    memory: MemoryPort
    pseudonymiser: Pseudonymiser
    extractor: RequirementExtractor = field(default_factory=RequirementExtractor)
    llm: LLMProvider | None = None
    #: Purpose → model. Optional so tests can omit it.
    routing: ModelRouting | None = None
    #: Layered limiter. The LLM scope is checked before any provider call.
    limiter: LayeredRateLimiter | None = None
    #: Operator emergency flags. `None` means nothing is ever paused.
    switches: KillSwitches | None = None
    #: Sanitised funnel events. Failures here never fail a turn.
    analytics: AnalyticsSink | None = None
    #: Shared cache for the candidate pool and geo lookups.
    cache: Cache | None = None
    #: Queue memory deeds through the outbox. Off unless a memory service is
    #: actually configured — otherwise every turn writes a row, the relay
    #: pushes it to SQS, and the outbound worker throws it away.
    record_deeds: bool = False
    #: Every LLM ceiling, keyed. Business numbers stay out of this module.
    budget: dict[str, int] = field(default_factory=dict)
    #: Deprecated alias kept so existing callers still work.
    llm_budget_tokens: int = 40_000
    metrics: MetricsEmitter | None = None

    def token_budget(self) -> TokenBudget:
        """A fresh per-conversation budget for one turn.

        Rebuilt per turn rather than carried across the conversation because
        the worker is stateless — the durable spend record is `llm_usage`, and
        re-deriving a running total from it on every message would cost a query
        to save a fraction of a cent.
        """
        return TokenBudget(
            limit=self.budget.get("tokens", self.llm_budget_tokens),
            max_calls_per_turn=self.budget.get("calls_per_turn", 2),
            max_calls_per_conversation=self.budget.get("calls_per_conversation", 12),
            max_escalations=self.budget.get("escalations", 1),
        )


class TurnService:
    """Processes one inbound envelope."""

    def __init__(self, deps: TurnDependencies) -> None:
        self._d = deps

    @property
    def conversations(self) -> ConversationStatePort:
        """Read access to conversation state.

        Exposed for the handoff layer's "is this turn already ours" check, which
        must run before any expensive work and therefore cannot go through a
        full turn.
        """
        return self._d.conversations

    async def handle(self, envelope: InboundEnvelope) -> TurnResult:
        metrics = self._d.metrics or MetricsEmitter(enabled=False)
        degraded = DegradedSources()

        # --- kill switch before anything else, including the idempotency claim.
        #
        # Ordering matters. Claiming first and then declining would burn the
        # dedup key: when the operator unpauses, the caller's redelivery of that
        # same message would be swallowed as a duplicate and the parent would
        # never get an answer. MATCHING_PAUSED is documented as lossless
        # (config/kill_switches.py) and that is only true if we take no
        # durable action at all.
        if await self._paused(Switch.MATCHING_PAUSED):
            metrics.count(Metric.KILL_SWITCH_ACTIVE)
            logger.warning("matching paused by kill switch; declining turn")
            snapshot = await self._d.conversations.load(envelope.conversation_id)
            return TurnResult(
                conversation_id=envelope.conversation_id,
                state=snapshot.state,
                reply=None,
                paused=True,
            )

        # --- idempotency: before any state mutation or side effect.
        if not await self._d.idempotency.claim(
            envelope.dedup_key, ttl_seconds=IDEMPOTENCY_TTL_SECONDS
        ):
            metrics.count(Metric.DUPLICATE_EVENTS)
            logger.info("duplicate event ignored", extra={"tmm_dedup_key": envelope.dedup_key})
            snapshot = await self._d.conversations.load(envelope.conversation_id)
            return TurnResult(
                conversation_id=envelope.conversation_id,
                state=snapshot.state,
                reply=None,
                duplicate=True,
            )

        try:
            return await self._process(envelope, metrics, degraded)
        except OptimisticLockError:
            # Another worker advanced this conversation. Release the claim so a
            # redelivery can legitimately retry rather than being eaten as a
            # duplicate.
            metrics.count(Metric.OPTIMISTIC_LOCK_CONFLICT)
            await self._d.idempotency.release(envelope.dedup_key)
            raise
        except Exception:
            await self._d.idempotency.release(envelope.dedup_key)
            raise

    # ------------------------------------------------------------- internals
    async def _process(
        self, envelope: InboundEnvelope, metrics: MetricsEmitter, degraded: DegradedSources
    ) -> TurnResult:
        now = datetime.now(UTC)
        conversation_id = envelope.conversation_id
        conversation_hash = self._d.pseudonymiser.conversation(conversation_id)
        snapshot = await self._d.conversations.load(conversation_id)
        expected_version = snapshot.lock_version

        payload = envelope.payload
        if isinstance(payload, ParentSelectionV1):
            return await self._handle_selection(
                payload, snapshot, expected_version, envelope, conversation_hash, metrics
            )

        metrics.count(Metric.MESSAGES_RECEIVED)

        # --- behavioural abuse controls, before any expensive work.
        #
        # These are *escalating* and deliberately slow to bite: one malformed
        # message never reaches a block (§9). A shed turn releases its
        # idempotency claim so the parent's next genuine message is not
        # mistaken for a duplicate.
        text_for_abuse = getattr(payload, "text", "") or ""
        assessment = await self._assess_abuse(conversation_hash, text_for_abuse, metrics)
        if assessment is not None and assessment.enforcement in _SHEDDING_ENFORCEMENTS:
            await self._d.idempotency.release(envelope.dedup_key)
            await self._emit(
                AnalyticsEventName.ABUSE_ENFORCED,
                conversation_hash,
                dimensions={"enforcement": assessment.enforcement.value},
            )
            reply = None if assessment.enforcement is Enforcement.COOLDOWN else THROTTLE_REPLY
            return TurnResult(
                conversation_id=conversation_id,
                state=snapshot.state,
                reply=reply,
                throttled=True,
            )

        snapshot, _ = try_transition(
            snapshot, Trigger.MESSAGE_RECEIVED, now=now, trace_id=envelope.trace_id
        )
        await self._emit(
            AnalyticsEventName.MATCH_STARTED,
            conversation_hash,
            dimensions={
                "source_agent": envelope.source_agent[:40],
                "turn_index": min(snapshot.turn_count, 50),
            },
        )

        # --- did the parent name a tutor rather than describe a need?
        #
        # Checked before extraction, because "tell me about Sneha Joshi" carries
        # no requirement at all and the extractor would answer it with "which
        # subject?" — which reads as not listening. A candidate name that
        # matches nobody falls straight through to matching, so a wrong guess
        # costs one indexed query and nothing else (orchestration/tutor_lookup.py).
        lookup_reply = await self._answer_tutor_lookup(payload, envelope, conversation_hash)
        if lookup_reply is not None:
            snapshot, _ = try_transition(
                snapshot, Trigger.REQUIREMENTS_INCOMPLETE, now=now, trace_id=envelope.trace_id
            )
            await self._d.conversations.save(snapshot, expected_version=expected_version)
            await self._enqueue_reply(envelope, lookup_reply, conversation_hash)
            return TurnResult(
                conversation_id=conversation_id,
                state=snapshot.state,
                reply=lookup_reply,
                degraded=degraded.as_tuple(),
                asked_for="tutor_lookup",
            )

        # --- this turn's requirement, and what other agents already know.
        #
        # In the deployed topology both were resolved by the internet-side
        # enrich worker and arrived on the envelope, because this function runs
        # in the VPC with no NAT Gateway and cannot reach OpenAI or the memory
        # service at all (contracts/enrichment.py). The inline path below is the
        # local stack and the test suite, where one process does everything.
        enrichment = envelope.enrichment
        if enrichment is not None:
            assert_supported(enrichment)
            incoming = enrichment.requirement
            recalled = dict(enrichment.memory_facts)
            for source in enrichment.degraded:
                degraded.mark(source)
            if enrichment.injection_detections:
                metrics.count(Metric.INJECTION_DETECTED, len(enrichment.injection_detections))
        else:
            with Timer() as stage:
                incoming = await self._extract(payload, conversation_id, now, degraded, metrics)
            metrics.timing(Metric.STAGE_EXTRACTION, stage.elapsed_ms)

            with Timer() as stage:
                recalled = await self._recall(conversation_hash, envelope.trace_id, degraded)
            metrics.timing(Metric.STAGE_MEMORY_LOOKUP, stage.elapsed_ms)

        merged = _apply_memory(incoming, recalled, now)

        with Timer() as stage:
            stored = await self._d.requirements.load(conversation_id)
            requirement = stored.merged_with(merged) if stored else merged
            await self._d.requirements.save(requirement)
        metrics.timing(Metric.STAGE_STATE_LOAD, stage.elapsed_ms)

        # --- Stage B: is anything essential still missing?
        readiness = assess(requirement)
        if not readiness.ready_to_match:
            snapshot, _ = try_transition(
                snapshot, Trigger.REQUIREMENTS_INCOMPLETE, now=now, trace_id=envelope.trace_id
            )
            await self._d.conversations.save(snapshot, expected_version=expected_version)
            question = readiness.next_question
            reply = question.render() if question else "Could you tell me a bit more?"
            # A clarifying question is generated too, and a template that
            # starts echoing the parent's message could carry PII straight back
            # out. Guard it on the same terms as a shortlist.
            reply = self._guard_output(reply, decision=None, metrics=metrics)
            await self._enqueue_reply(envelope, reply, conversation_hash)
            await self._record_deed(
                DeedType.REQUIREMENT_CAPTURED,
                envelope,
                conversation_hash,
                summary=f"asked for {question.field}" if question else "asked for more detail",
                degraded=degraded,
            )
            await self._emit(
                AnalyticsEventName.CLARIFICATION_ASKED,
                conversation_hash,
                dimensions={"asked_for": (question.field if question else "unknown")[:40]},
            )
            return TurnResult(
                conversation_id=conversation_id,
                state=snapshot.state,
                reply=reply,
                degraded=degraded.as_tuple(),
                asked_for=question.field if question else None,
            )

        # --- Stages C-H
        snapshot, _ = try_transition(
            snapshot, Trigger.REQUIREMENTS_COMPLETE, now=now, trace_id=envelope.trace_id
        )
        snapshot, _ = try_transition(
            snapshot, Trigger.MATCH_STARTED, now=now, trace_id=envelope.trace_id
        )
        metrics.count(Metric.MATCH_REQUESTS)

        with Timer() as timer:
            outcome = await self._d.orchestrator.match(
                requirement, trace_id=envelope.trace_id, now=now, degraded=degraded
            )
        metrics.timing(Metric.SHORTLIST_LATENCY, timer.elapsed_ms)
        metrics.put(Metric.CANDIDATE_POOL_SIZE, len(outcome.decision.candidate_ids))
        metrics.put(Metric.SHORTLIST_SIZE, len(outcome.decision.shortlist))
        metrics.put(Metric.HARD_FILTER_REJECTIONS, len(outcome.decision.rejections))
        if outcome.fabrication_violations:
            metrics.count(Metric.FABRICATION_VIOLATIONS, len(outcome.fabrication_violations))
        if outcome.guard_refusals:
            metrics.count(Metric.GUARD_REFUSALS, len(outcome.guard_refusals))

        await self._d.decisions.save(outcome.decision)

        if outcome.matched:
            trigger = Trigger.SHORTLIST_GENERATED
            deed = DeedType.SHORTLIST_GENERATED
            summary = f"shortlisted {len(outcome.decision.shortlist)} tutors"
        else:
            trigger = Trigger.NO_CANDIDATES
            deed = DeedType.FAILED_NO_CANDIDATES
            summary = outcome.decision.no_match_reason or "no candidates"
            metrics.count(Metric.NO_MATCH)

        snapshot, _ = try_transition(snapshot, trigger, now=now, trace_id=envelope.trace_id)

        if outcome.needs_human:
            metrics.count(Metric.HUMAN_HANDOFF)
            snapshot, _ = try_transition(
                snapshot, Trigger.HUMAN_HANDOFF, now=now, trace_id=envelope.trace_id
            )
            deed = DeedType.HUMAN_HANDOFF_REQUESTED
        elif outcome.matched:
            snapshot, _ = try_transition(
                snapshot, Trigger.SHORTLIST_DELIVERED, now=now, trace_id=envelope.trace_id
            )

        with Timer() as stage:
            await self._d.conversations.save(snapshot, expected_version=expected_version)
        metrics.timing(Metric.STAGE_PERSISTENCE, stage.elapsed_ms)

        # --- the last gate. Runs on the assembled bytes, after every template
        #     and every model has had its say, with no path back to either.
        reply = self._guard_output(outcome.message, decision=outcome.decision, metrics=metrics)

        with Timer() as stage:
            await self._enqueue_reply(envelope, reply, conversation_hash)
        metrics.timing(Metric.STAGE_OUTBOUND_ENQUEUE, stage.elapsed_ms)

        await self._record_deed(
            deed,
            envelope,
            conversation_hash,
            summary=summary,
            degraded=degraded,
            tutor_ids=[e.tutor_id for e in outcome.decision.shortlist],
        )
        await self._emit_outcome(outcome, requirement, conversation_hash, degraded)
        if degraded.as_tuple():
            metrics.count(Metric.DEGRADED_TURN)

        return TurnResult(
            conversation_id=conversation_id,
            state=snapshot.state,
            reply=reply,
            outcome=outcome,
            degraded=degraded.as_tuple(),
        )

    def _guard_output(
        self,
        message: str,
        *,
        decision: MatchDecisionV1 | None,
        metrics: MetricsEmitter,
        authorised_fees: tuple[str, ...] = (),
    ) -> str:
        """Validate a finished message; substitute a safe fallback if it fails.

        A rejection is an error-level event, not a warning: it means a template
        or a model produced something we could not verify, and someone has to
        look at it.
        """
        guarded, verdict = output_guard.enforce(
            message,
            decision=decision,
            public_base_url=self._d.orchestrator.public_base_url,
            authorised_fees=authorised_fees,
        )
        if not verdict.ok:
            metrics.count(Metric.OUTPUT_GUARD_REJECTED, len(verdict.violations))
        return guarded

    async def _handle_selection(
        self,
        payload: ParentSelectionV1,
        snapshot: ConversationSnapshot,
        expected_version: int,
        envelope: InboundEnvelope,
        conversation_hash: str,
        metrics: MetricsEmitter,
    ) -> TurnResult:
        now = datetime.now(UTC)
        metrics.count(Metric.MATCH_ACCEPTED)
        snapshot, moved = try_transition(
            snapshot, Trigger.PARENT_SELECTED, now=now, trace_id=envelope.trace_id
        )
        if not moved:
            # A selection arriving in a state that never sent a shortlist. Fail
            # safely: acknowledge, change nothing.
            logger.warning(
                "selection in unexpected state", extra={"tmm_state": snapshot.state.value}
            )
            return TurnResult(
                conversation_id=envelope.conversation_id, state=snapshot.state, reply=None
            )

        await self._d.conversations.save(snapshot, expected_version=expected_version)
        if payload.demo_requested:
            metrics.count(Metric.DEMO_REQUESTED)
        await self._record_deed(
            DeedType.CANDIDATE_SELECTED,
            envelope,
            conversation_hash,
            summary="parent selected a tutor",
            degraded=DegradedSources(),
        )
        reply = "Great choice — I'll arrange a demo class and confirm the timing shortly."
        await self._enqueue_reply(envelope, reply, conversation_hash)
        return TurnResult(
            conversation_id=envelope.conversation_id, state=snapshot.state, reply=reply
        )

    # ------------------------------------------------------- guards and taps
    async def _paused(self, switch: Switch) -> bool:
        """Read a kill switch. An unreadable switch is not a reason to stop.

        The store keeps its last known state (`PostgresKillSwitchStore`), so
        this only returns False when no switch layer is configured at all.
        """
        if self._d.switches is None:
            return False
        try:
            return await self._d.switches.is_paused(switch)
        except Exception:
            logger.warning("kill switch read failed", extra={"tmm_switch": switch.value})
            return False

    async def _assess_abuse(
        self, conversation_ref: str, text: str, metrics: MetricsEmitter
    ) -> Any | None:
        """Behavioural signals. Needs a cache; degrades to no-op without one."""
        if self._d.cache is None or not text.strip():
            return None
        try:
            assessment = await AbuseDetector(self._d.cache).assess(conversation_ref, text)
        except Exception:
            logger.warning("abuse assessment failed; allowing the turn")
            return None
        if not assessment.suspicious:
            return None
        metrics.count(Metric.ABUSE_SIGNAL, len(assessment.signals))
        if any(s.kind == "contact_harvest_attempt" for s in assessment.signals):
            # Counted, never blocked on its own. A parent asking "can I call
            # the tutor" is not an attacker, and the real defence is that the
            # contact details are not in the model payload at all.
            metrics.count(Metric.CONTACT_HARVEST_ATTEMPT)
        if assessment.enforcement is not Enforcement.ALLOW:
            metrics.count(Metric.ABUSE_ENFORCEMENT)
            logger.warning(
                "abuse enforcement applied",
                extra={
                    "tmm_enforcement": assessment.enforcement.value,
                    "tmm_signals": [s.kind for s in assessment.signals],
                },
            )
        return assessment

    def _guarded_llm(
        self, conversation_ref: str, budget: TokenBudget
    ) -> tuple[Any | None, UsageLedger]:
        """Wrap the raw provider so no call escapes the pre-spend checks.

        Built per turn because the budget and ledger are per turn. The
        underlying client — and therefore its connection pool and circuit
        breaker — is the shared one from the composition root.
        """
        ledger = UsageLedger(conversation_ref=conversation_ref)
        if self._d.llm is None:
            return None, ledger

        limiter = self._d.limiter
        switches = self._d.switches

        async def rate_check(key: str) -> bool:
            if limiter is None:
                return True
            decision = await limiter.check(LimitScope.LLM, key)
            return decision.allowed

        async def pause_check() -> bool:
            if switches is None:
                return False
            return await switches.is_paused(Switch.LLM_PAUSED)

        guarded = GuardedProvider(
            self._d.llm,
            routing=self._d.routing or routing_from_settings(object()),
            budget=budget,
            ledger=ledger,
            rate_check=rate_check if limiter is not None else None,
            pause_check=pause_check if switches is not None else None,
            rate_scope_key=conversation_ref,
        )
        return guarded, ledger

    async def _emit(
        self,
        name: AnalyticsEventName,
        conversation_ref: str,
        *,
        dimensions: dict[str, Any] | None = None,
        match_session_id: str | None = None,
        policy_ref: str | None = None,
    ) -> None:
        """Fire-and-forget funnel event. Never fails a turn."""
        if self._d.analytics is None:
            return
        try:
            await self._d.analytics.emit(
                AnalyticsEventV1(
                    name=name,
                    conversation_ref=conversation_ref,
                    match_session_id=match_session_id,
                    policy_ref=policy_ref,
                    dimensions=dict(dimensions or {}),
                )
            )
        except Exception:
            # Includes `UnsafeDimension`: a dimension that would have leaked is
            # dropped here rather than raised into a parent's turn, but it is
            # logged at warning so the mistake is visible in CI and staging.
            logger.warning("analytics event rejected", extra={"tmm_event": name.value})

    async def _emit_outcome(
        self,
        outcome: MatchOutcome,
        requirement: MatchRequirementV1,
        conversation_ref: str,
        degraded: DegradedSources,
    ) -> None:
        decision = outcome.decision
        coverage = max((c.weight_coverage for c in decision.scored), default=0.0)
        dimensions: dict[str, Any] = {
            "subject": str(requirement.value_of("subject") or "unknown")[:40],
            "board": str(requirement.value_of("board") or "unknown")[:40],
            "class_band": class_band(requirement.value_of("student_class")),
            "mode": str(requirement.value_of("mode") or "unknown")[:20],
            "city": str(requirement.location.city or "unknown")[:40],
            "urgency": requirement.urgency.value,
            "shortlist_size": len(decision.shortlist),
            "candidate_pool_size": len(decision.candidate_ids),
            "weight_coverage_band": coverage_band(coverage),
            "requires_human_review": decision.requires_human_review,
            "degraded": ",".join(degraded.as_tuple())[:60],
        }
        if decision.matched:
            name = AnalyticsEventName.SHORTLIST_GENERATED
        else:
            name = AnalyticsEventName.NO_CANDIDATE
            dimensions["no_match_reason"] = str(decision.no_match_reason or "unknown")[:60]
        await self._emit(
            name,
            conversation_ref,
            dimensions=dimensions,
            match_session_id=decision.match_session_id,
            policy_ref=outcome.policy.ref,
        )
        if decision.requires_human_review:
            await self._emit(
                AnalyticsEventName.HUMAN_HANDOFF,
                conversation_ref,
                dimensions={"no_match_reason": str(decision.no_match_reason or "review")[:60]},
                match_session_id=decision.match_session_id,
            )

    async def _extract(
        self,
        payload: LeadEventV1 | WhatsAppTurnV1 | ParentSelectionV1,
        conversation_id: str,
        now: datetime,
        degraded: DegradedSources,
        metrics: MetricsEmitter,
    ) -> MatchRequirementV1:
        if isinstance(payload, LeadEventV1):
            return requirement_from_lead_event(
                conversation_id=conversation_id,
                lead_id=payload.lead_id,
                subject=payload.subject,
                board=payload.board,
                student_class=payload.student_class,
                city=payload.city,
                tuition_mode=payload.tuition_mode,
                confidence=payload.confidence_score,
                now=now,
            )
        if isinstance(payload, WhatsAppTurnV1):
            budget = self._d.token_budget()
            budget.begin_turn()
            conversation_ref = self._d.pseudonymiser.conversation(conversation_id)
            provider, ledger = self._guarded_llm(conversation_ref, budget)
            outcome = await self._d.extractor.extract(
                payload.text,
                conversation_id=conversation_id,
                provider=provider,
                budget=budget,
                now=now,
            )
            ledger.emit(metrics)
            if outcome.injection_detections:
                metrics.count(Metric.INJECTION_DETECTED, len(outcome.injection_detections))
            if outcome.degraded:
                degraded.mark("llm")
                if outcome.llm_error == "LLMPaused":
                    metrics.count(Metric.LLM_PAUSED)
                elif outcome.llm_error == "LLMBudgetExceeded":
                    metrics.count(Metric.LLM_BUDGET_EXCEEDED)
            return outcome.requirement
        return MatchRequirementV1(conversation_id=conversation_id, captured_at=now)

    async def _answer_tutor_lookup(
        self,
        payload: LeadEventV1 | WhatsAppTurnV1 | ParentSelectionV1,
        envelope: InboundEnvelope,
        conversation_hash: str,
    ) -> str | None:
        """Answer "who is X" / "is X available", or None to carry on matching.

        Returns None on *every* uncertain path — no name found, nobody matches,
        the lookup errored — so this can only ever add an answer, never remove
        one. That is what makes it safe to run ahead of extraction.
        """
        if not isinstance(payload, WhatsAppTurnV1):
            return None
        lookup = tutor_lookup.detect(payload.text)
        if lookup is None:
            return None

        try:
            found = await self._d.orchestrator.find_by_name(lookup.terms)
        except Exception:
            # A directory question is never worth failing a turn over.
            logger.warning("tutor name lookup failed; falling through to matching")
            return None
        if not found:
            return None

        # `asked_for` only. A match count is not on `ALLOWED_DIMENSIONS`, and
        # widening that allowlist for a debugging nicety is the wrong trade.
        await self._emit(
            AnalyticsEventName.CLARIFICATION_ASKED,
            conversation_hash,
            dimensions={"asked_for": "tutor_lookup"},
        )

        if len(found) == 1:
            tutor = found[0]
            body = tutor_lookup.summarise(
                tutor, profile_url=self._d.orchestrator.profile_link(tutor)
            )
            tail = (
                "\n\nTell me the class and subject and I'll check they're the right fit."
                if lookup.wants_summary
                else "\n\nWant me to check they're free for your class and subject?"
            )
            return self._guard_output(
                body + tail,
                decision=None,
                metrics=MetricsEmitter(enabled=False),
                # The one band we are entitled to quote here: this tutor's own.
                authorised_fees=(tutor.fee.label or "",) if tutor.fee else (),
            )

        # Several people share the name. Listing them is the honest answer;
        # picking one would be a guess presented as a fact.
        lines = [f"I found {len(found)} tutors matching that name:", ""]
        for tutor in found[:MAX_LOOKUP_RESULTS]:
            where = tutor.locality or tutor.city
            subject = ", ".join(tutor.capabilities.subjects[:2]) or None
            detail = " · ".join(part for part in (where, subject) if part)
            lines.append(f"*{tutor.name}*" + (f"\n{detail}" if detail else ""))
            link = self._d.orchestrator.profile_link(tutor)
            if link:
                lines.append(link)
            lines.append("")
        lines.append("Which one did you mean?")
        return self._guard_output(
            "\n".join(lines), decision=None, metrics=MetricsEmitter(enabled=False)
        )

    async def _recall(
        self, conversation_hash: str, trace_id: str, degraded: DegradedSources
    ) -> dict[str, str]:
        """Recall permitted memory. An outage is a degraded source, not a failure."""
        packet = await self._d.memory.recall(
            entity_type="conversation",
            entity_id=conversation_hash,
            purpose="tutor_matching",
            trace_id=trace_id,
        )
        if packet.degraded:
            degraded.mark("chitragupta")
            return {}
        # Stale or low-confidence memory is dropped rather than trusted: a
        # remembered "Class 9" from last year is wrong this year.
        return {
            fact.key: fact.value
            for fact in packet.facts
            if fact.confidence >= 0.6 and not fact.denied and fact.value
        }

    async def _enqueue_reply(
        self, envelope: InboundEnvelope, text: str, conversation_hash: str
    ) -> None:
        """Hand the reply to the outbound worker via the transactional outbox.

        Enqueued rather than sent inline so a Meta outage retries delivery
        without ever re-running the match.

        The row is written in both ownership modes, but as two different kinds.
        When we own delivery it is a `whatsapp_reply` carrying the recipient.
        When the caller owns delivery there is no recipient on the envelope —
        the inbound contract only ever carries `phone_hash` — so it is a
        `caller_delivered_reply`: the audit record of what the parent was told,
        which the relay closes without sending. Writing an addressable-looking
        row with no address would create a permanent DLQ resident instead.
        """
        deliverable = bool(envelope.reply_to)
        delivery = OutboundDeliveryV1(
            kind=OUTBOX_REPLY if deliverable else OUTBOX_CALLER_REPLY,
            recipient=envelope.reply_to,
            body=text[:OUTBOUND_MAX_BODY_CHARS],
            dedup_key=f"reply:{envelope.dedup_key}",
            trace_id=envelope.trace_id,
            conversation_ref=conversation_hash,
        )
        await self._d.outbox.enqueue(
            OutboxMessage(
                kind=delivery.kind,
                conversation_id=envelope.conversation_id,
                payload=delivery.to_payload(),
                dedup_key=delivery.dedup_key,
                trace_id=envelope.trace_id,
            )
        )

    async def _record_deed(
        self,
        deed_type: str,
        envelope: InboundEnvelope,
        conversation_hash: str,
        *,
        summary: str,
        degraded: DegradedSources,
        tutor_ids: list[str] | None = None,
    ) -> None:
        """Queue the deed. Never calls the memory service from this process.

        Skipped entirely when no memory service is configured: an outbox row,
        an SQS message and a worker invocation to reach a no-op is a real cost
        for no behaviour.

        This function runs in the VPC with no NAT Gateway, so an inline HTTP
        call here would hang until its timeout on every single turn. The row is
        written in the same transaction-adjacent outbox as the reply, and the
        internet-side outbound worker delivers it — which also means a memory
        outage now retries instead of losing the deed.
        """
        if not self._d.record_deeds:
            return

        lead_id = getattr(envelope.payload, "lead_id", None)
        deed = MemoryDeedV1(
            deed_type=deed_type,
            summary=summary[:500],
            entity_scope=entity_scope(
                conversation_hash=conversation_hash, lead_id=lead_id, tutor_ids=tutor_ids
            ),
            dedup_key=f"deed:{deed_type}:{envelope.dedup_key}"[:128],
            trace_id=envelope.trace_id,
            conversation_ref=conversation_hash,
        )
        try:
            await self._d.outbox.enqueue(
                OutboxMessage(
                    kind=OUTBOX_KIND_DEED,
                    conversation_id=envelope.conversation_id,
                    payload=deed.to_payload(),
                    dedup_key=deed.dedup_key,
                    trace_id=envelope.trace_id,
                )
            )
        except Exception:
            # A deed is an audit nicety, never the parent's answer. Losing one
            # degrades the turn; it must not fail it.
            logger.warning("could not queue deed", extra={"tmm_deed": deed_type})
            degraded.mark("chitragupta")


def _apply_memory(
    requirement: MatchRequirementV1, recalled: dict[str, str], now: datetime
) -> MatchRequirementV1:
    """Overlay recalled facts at MEMORY provenance.

    Memory ranks below anything this conversation established, so a remembered
    preference fills a gap but never overwrites what the parent just said.
    """
    if not recalled:
        return requirement

    def tracked(key: str) -> Tracked[str] | None:
        value = recalled.get(key)
        if not value:
            return None
        return Tracked(value=value, confidence=0.7, provenance=Provenance.MEMORY, observed_at=now)

    overlay = MatchRequirementV1(
        conversation_id=requirement.conversation_id,
        subject=tracked("subject"),
        board=tracked("board"),
        student_class=tracked("student_class"),
        language_preference=tracked("language_preference"),
        preferred_teaching_style=tracked("teaching_style"),
    )
    return overlay.merged_with(requirement)

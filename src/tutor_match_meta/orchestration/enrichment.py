"""The internet-side half of a turn. Runs outside the VPC, touches no database.

Everything in here needs a route to the public internet — OpenAI for
requirement extraction, the memory service for what other agents already
know. Neither can run in the match worker, because that function is attached
to private subnets with no NAT Gateway and has no route off the VPC at all.

Geocoding is deliberately *not* here. Its primary backend reads the
`geo_point` table, so it belongs on the database side; the paid HTTP backend
is refused outright in a deployed environment (see config/settings.py) for
the same NAT reason, and the offline sync job pre-populates `geo_point`
instead.

The rule this module enforces is simple and worth stating plainly: **no
database handle is ever passed in here.** `EnrichmentService` takes providers,
not sessions. If a future change needs a row, it belongs on the other side of
the queue.

Failure is never fatal. Every dependency here is optional by design, and a turn
that arrives with nothing enriched still produces a deterministic shortlist —
that is what `degraded` records, and what the fallback matrix promises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tutor_match_meta.config.kill_switches import KillSwitches, Switch
from tutor_match_meta.contracts.enrichment import MAX_MEMORY_FACTS, EnrichmentV1
from tutor_match_meta.contracts.inbound import (
    InboundEnvelope,
    LeadEventV1,
    ParentSelectionV1,
    WhatsAppTurnV1,
)
from tutor_match_meta.contracts.requirement import MatchRequirementV1
from tutor_match_meta.integrations.llm.provider import LLMProvider, TokenBudget
from tutor_match_meta.integrations.llm.routing import (
    GuardedProvider,
    ModelRouting,
    UsageLedger,
    routing_from_settings,
)
from tutor_match_meta.observability.context import Timer, get_logger
from tutor_match_meta.observability.metrics import Metric, MetricsEmitter
from tutor_match_meta.orchestration.extraction import (
    RequirementExtractor,
    requirement_from_lead_event,
)
from tutor_match_meta.repositories.ports import DegradedSources, MemoryPort
from tutor_match_meta.security.pii import Pseudonymiser
from tutor_match_meta.security.rate_limit import LayeredRateLimiter, LimitScope

logger = get_logger("enrich")


@dataclass(slots=True)
class EnrichmentDependencies:
    """Internet-reachable collaborators only. Deliberately no session factory."""

    pseudonymiser: Pseudonymiser
    extractor: RequirementExtractor = field(default_factory=RequirementExtractor)
    llm: LLMProvider | None = None
    routing: ModelRouting | None = None
    #: Optional: recalls what other agents know. Degrades to no memory.
    memory: MemoryPort | None = None
    #: LLM scope limiter. Checked before any provider call leaves the process.
    limiter: LayeredRateLimiter | None = None
    switches: KillSwitches | None = None
    budget: dict[str, int] = field(default_factory=dict)
    metrics: MetricsEmitter | None = None

    def token_budget(self) -> TokenBudget:
        return TokenBudget(
            limit=self.budget.get("tokens", 40_000),
            max_calls_per_turn=self.budget.get("calls_per_turn", 2),
            max_calls_per_conversation=self.budget.get("calls_per_conversation", 12),
            max_escalations=self.budget.get("escalations", 1),
        )


class EnrichmentService:
    """Turns a raw inbound envelope into one carrying an `EnrichmentV1`."""

    def __init__(self, deps: EnrichmentDependencies) -> None:
        self._d = deps

    async def enrich(self, envelope: InboundEnvelope) -> InboundEnvelope:
        """Attach the enrichment. Never raises for a dependency failure."""
        metrics = self._d.metrics or MetricsEmitter(enabled=False)
        degraded = DegradedSources()
        now = datetime.now(UTC)
        conversation_id = envelope.conversation_id
        conversation_ref = self._d.pseudonymiser.conversation(conversation_id)

        payload = envelope.payload
        if isinstance(payload, ParentSelectionV1):
            # A selection carries no free text and needs nothing resolved.
            return envelope

        with Timer() as stage:
            requirement, used_llm, llm_error, detections = await self._extract(
                payload, conversation_id, conversation_ref, now, degraded, metrics
            )
        metrics.timing(Metric.STAGE_EXTRACTION, stage.elapsed_ms)

        with Timer() as stage:
            facts = await self._recall(conversation_ref, envelope.trace_id, degraded)
        metrics.timing(Metric.STAGE_MEMORY_LOOKUP, stage.elapsed_ms)

        enrichment = EnrichmentV1(
            requirement=requirement,
            memory_facts=facts,
            degraded=degraded.as_tuple(),
            injection_detections=detections,
            used_llm=used_llm,
            llm_error=llm_error,
            enriched_at=now,
        )
        metrics.flush()
        return envelope.model_copy(update={"enrichment": enrichment})

    # ------------------------------------------------------------- internals
    async def _extract(
        self,
        payload: LeadEventV1 | WhatsAppTurnV1,
        conversation_id: str,
        conversation_ref: str,
        now: datetime,
        degraded: DegradedSources,
        metrics: MetricsEmitter,
    ) -> tuple[MatchRequirementV1, bool, str | None, tuple[str, ...]]:
        if isinstance(payload, LeadEventV1):
            requirement = requirement_from_lead_event(
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
            return requirement, False, None, ()

        budget = self._d.token_budget()
        budget.begin_turn()
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

        return (
            outcome.requirement,
            outcome.used_llm,
            outcome.llm_error,
            tuple(str(d) for d in outcome.injection_detections),
        )

    def _guarded_llm(
        self, conversation_ref: str, budget: TokenBudget
    ) -> tuple[Any | None, UsageLedger]:
        """Same guard stack the match worker used to apply, unchanged: kill
        switch, then budget, then rate limit, then the provider."""
        ledger = UsageLedger(conversation_ref=conversation_ref)
        if self._d.llm is None:
            return None, ledger

        limiter, switches = self._d.limiter, self._d.switches

        async def rate_check(key: str) -> bool:
            if limiter is None:
                return True
            return (await limiter.check(LimitScope.LLM, key)).allowed

        async def pause_check() -> bool:
            return False if switches is None else await switches.is_paused(Switch.LLM_PAUSED)

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

    async def _recall(
        self, conversation_ref: str, trace_id: str, degraded: DegradedSources
    ) -> dict[str, str]:
        if self._d.memory is None:
            return {}
        try:
            packet = await self._d.memory.recall(
                entity_type="conversation",
                entity_id=conversation_ref,
                purpose="tutor_matching",
                trace_id=trace_id,
            )
        except Exception:
            logger.warning("memory recall failed")
            degraded.mark("chitragupta")
            return {}

        if packet.degraded:
            degraded.mark("chitragupta")
            return {}

        # Stale or low-confidence memory is dropped rather than trusted: a
        # remembered "Class 9" from last year is wrong this year.
        facts = {
            fact.key: fact.value
            for fact in packet.facts
            if fact.confidence >= 0.6 and not fact.denied and fact.value
        }
        # Bounded here as well as in the contract, so an oversized packet
        # degrades to a truncated recall rather than a rejected envelope.
        if len(facts) > MAX_MEMORY_FACTS:
            logger.warning("memory packet truncated", extra={"tmm_facts": len(facts)})
            facts = dict(list(facts.items())[:MAX_MEMORY_FACTS])
        return facts

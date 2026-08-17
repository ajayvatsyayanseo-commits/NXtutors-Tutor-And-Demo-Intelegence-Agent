"""Metrics via CloudWatch Embedded Metric Format.

EMF means metrics are emitted as structured log lines — no PutMetricData call,
no extra latency on the request path, no IAM permission beyond writing logs. The
same line is both the log and the metric.

Dimension values are validated against `pii.FORBIDDEN_IN_METRIC_LABELS` at
runtime. That guard exists because a high-cardinality identifier in a dimension
is both a privacy leak and a genuinely expensive CloudWatch mistake.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from enum import StrEnum

from tutor_match_meta.security.pii import assert_label_safe

NAMESPACE = "NXTutors/TutorMatchMeta"


class Unit(StrEnum):
    COUNT = "Count"
    MILLISECONDS = "Milliseconds"
    PERCENT = "Percent"
    NONE = "None"


class Metric(StrEnum):
    """Every metric this service emits. One place, so dashboards cannot drift."""

    # throughput
    MATCH_REQUESTS = "MatchRequests"
    MESSAGES_RECEIVED = "MessagesReceived"
    DUPLICATE_EVENTS = "DuplicateEvents"
    RATE_LIMITED = "RateLimited"
    # matching quality
    REQUIREMENT_COMPLETION_RATE = "RequirementCompletionRate"
    CANDIDATE_POOL_SIZE = "CandidatePoolSize"
    SHORTLIST_SIZE = "ShortlistSize"
    NO_MATCH = "NoMatch"
    HARD_FILTER_REJECTIONS = "HardFilterRejections"
    WEIGHT_COVERAGE = "WeightCoverage"
    FABRICATION_VIOLATIONS = "FabricationViolations"
    GUARD_REFUSALS = "GuardRefusals"
    # latency — end to end
    SHORTLIST_LATENCY = "ShortlistLatencyMs"
    TURN_LATENCY = "TurnLatencyMs"
    DB_LATENCY = "DbLatencyMs"
    RAG_LATENCY = "RagLatencyMs"
    LLM_LATENCY = "LlmLatencyMs"
    # latency — per stage (§24). Queue wait is separate from processing on
    # purpose: hiding it inside a generic "latency" is how a backlog looks
    # healthy right up until parents start complaining.
    STAGE_QUEUE_WAIT = "StageQueueWaitMs"
    STAGE_INGRESS_VALIDATION = "StageIngressValidationMs"
    STAGE_STATE_LOAD = "StageStateLoadMs"
    STAGE_MEMORY_LOOKUP = "StageMemoryLookupMs"
    STAGE_EXTRACTION = "StageExtractionMs"
    STAGE_CANDIDATE_SQL = "StageCandidateSqlMs"
    STAGE_SKILL_SCORING = "StageSkillScoringMs"
    STAGE_RAG = "StageRagMs"
    STAGE_EXPLANATION = "StageExplanationMs"
    STAGE_PERSISTENCE = "StagePersistenceMs"
    STAGE_OUTBOUND_ENQUEUE = "StageOutboundEnqueueMs"
    # cost
    LLM_CALLS = "LlmCalls"
    LLM_TOKENS = "LlmTokens"
    LLM_COST_MICROS = "LlmCostMicros"
    LLM_BUDGET_EXCEEDED = "LlmBudgetExceeded"
    LLM_PAUSED = "LlmPaused"
    PROMPT_CACHE_HIT_RATE = "PromptCacheHitRate"
    EMBEDDINGS_SKIPPED = "EmbeddingsSkipped"
    EMBEDDINGS_COMPUTED = "EmbeddingsComputed"
    EMBEDDING_COST_MICROS = "EmbeddingCostMicros"
    GEOCODE_CALLS = "GeocodeCalls"
    GEOCODE_CACHE_HITS = "GeocodeCacheHits"
    # dependencies
    CACHE_HIT_RATIO = "CacheHitRatio"
    CACHE_ERRORS = "CacheErrors"
    PROJECTION_STALENESS_HOURS = "ProjectionStalenessHours"
    CHITRAGUPTA_FAILURES = "ChitraguptaFailures"
    WEBSITE_FAILURES = "WebsiteFailures"
    FEED_FAILURES = "TutorFeedFailures"
    CIRCUIT_OPEN = "CircuitOpen"
    DEGRADED_TURN = "DegradedTurn"
    # operations
    QUEUE_AGE_SECONDS = "QueueAgeSeconds"
    DLQ_DEPTH = "DlqDepth"
    HUMAN_HANDOFF = "HumanHandoff"
    OUTBOX_PENDING = "OutboxPending"
    OUTBOX_RECLAIMED = "OutboxReclaimed"
    OUTBOX_DEAD = "OutboxDead"
    INVALID_TRANSITION = "InvalidTransition"
    OPTIMISTIC_LOCK_CONFLICT = "OptimisticLockConflict"
    KILL_SWITCH_ACTIVE = "KillSwitchActive"
    # abuse
    ABUSE_SIGNAL = "AbuseSignal"
    ABUSE_ENFORCEMENT = "AbuseEnforcement"
    CONTACT_HARVEST_ATTEMPT = "ContactHarvestAttempt"
    # outcomes
    MATCH_ACCEPTED = "MatchAccepted"
    DEMO_REQUESTED = "DemoRequested"
    INJECTION_DETECTED = "InjectionDetected"
    REPLACEMENT_REQUESTED = "ReplacementRequested"
    OUTPUT_GUARD_REJECTED = "OutputGuardRejected"


@dataclass(frozen=True, slots=True)
class MetricValue:
    metric: Metric
    value: float
    unit: Unit = Unit.COUNT


class MetricsEmitter:
    """Buffers metrics for one request and flushes a single EMF line.

    One line per request rather than per metric: fewer log events, and every
    metric from a request shares its dimensions and trace automatically.
    """

    def __init__(self, *, namespace: str = NAMESPACE, enabled: bool = True) -> None:
        self._namespace = namespace
        self._enabled = enabled
        self._values: dict[str, tuple[float, Unit]] = {}
        self._dimensions: dict[str, str] = {}
        self._properties: dict[str, object] = {}

    def with_dimensions(self, **labels: str) -> MetricsEmitter:
        """Set dimensions. Rejects anything identifying."""
        assert_label_safe(labels)
        self._dimensions.update({k: str(v)[:64] for k, v in labels.items() if v})
        return self

    def put(self, metric: Metric, value: float, unit: Unit = Unit.COUNT) -> None:
        existing = self._values.get(metric.value)
        # Counts accumulate within a request; gauges take the latest reading.
        if existing and unit is Unit.COUNT:
            self._values[metric.value] = (existing[0] + value, unit)
        else:
            self._values[metric.value] = (value, unit)

    def count(self, metric: Metric, value: float = 1.0) -> None:
        self.put(metric, value, Unit.COUNT)

    def timing(self, metric: Metric, milliseconds: float) -> None:
        self.put(metric, milliseconds, Unit.MILLISECONDS)

    def property(self, key: str, value: object) -> None:
        """A non-dimension searchable field. Still must not carry raw PII."""
        assert_label_safe({key: ""})
        self._properties[key] = value

    def flush(self) -> dict[str, object] | None:
        if not self._enabled or not self._values:
            return None
        payload: dict[str, object] = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [list(self._dimensions)] if self._dimensions else [[]],
                        "Metrics": [
                            {"Name": name, "Unit": unit.value}
                            for name, (_, unit) in self._values.items()
                        ],
                    }
                ],
            },
            **self._dimensions,
            **self._properties,
            **{name: value for name, (value, _) in self._values.items()},
        }
        from tutor_match_meta.observability.context import current

        context = current()
        if context is not None:
            payload["trace_id"] = context.trace_id
        sys.stdout.write(json.dumps(payload, default=str, separators=(",", ":")) + "\n")
        self._values.clear()
        return payload


#: Per-1k-token prices in micro-USD. Configuration, not a claim of current
#: pricing — the deploy pipeline overrides these from SSM, and they exist so a
#: cost graph is available at all rather than to be authoritative.
DEFAULT_TOKEN_COST_MICROS: dict[str, tuple[int, int]] = {
    "gpt-4o-mini": (150, 600),
    "gpt-4o": (2_500, 10_000),
}


def estimate_cost_micros(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: dict[str, tuple[int, int]] | None = None,
) -> int:
    """Estimated micro-USD for one call. Unknown models cost 0 and are flagged."""
    table = prices or DEFAULT_TOKEN_COST_MICROS
    rates = table.get(model)
    if rates is None:
        return 0
    prompt_rate, completion_rate = rates
    return round(prompt_tokens / 1000 * prompt_rate + completion_tokens / 1000 * completion_rate)

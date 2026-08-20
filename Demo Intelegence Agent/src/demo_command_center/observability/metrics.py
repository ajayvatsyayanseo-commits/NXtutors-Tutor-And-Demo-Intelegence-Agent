"""CloudWatch metrics via Embedded Metric Format.

EMF rather than `PutMetricData` for one reason that shows up on the bill: EMF is
a structured log line the agent already ships, so a metric costs nothing extra
per invocation and never adds latency or an IAM call to the request path.
`PutMetricData` is a synchronous API call per emission.

Every label goes through `assert_label_safe`. High-cardinality dimensions are
how a CloudWatch bill becomes larger than the compute bill, and identifying
dimensions are a privacy incident; both are rejected at the call.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TextIO

from demo_command_center.security.pii import assert_label_safe

NAMESPACE: Final = "NXTutors/DemoCommandCenter"
#: CloudWatch rejects a dimension set larger than this outright.
MAX_DIMENSIONS: Final = 9


class Unit(StrEnum):
    COUNT = "Count"
    MILLISECONDS = "Milliseconds"
    SECONDS = "Seconds"
    PERCENT = "Percent"
    NONE = "None"


class Metric(StrEnum):
    """The closed set of metric names.

    Closed on purpose: a typo in a metric name creates a *new* metric that no
    alarm watches, and the alarm on the correctly-spelled one goes quiet, which
    reads exactly like "the problem stopped".
    """

    WEBHOOK_ACCEPTED = "WebhookAccepted"
    #: A parent message that reached a trigger, and one that did not.
    #: The second is the signal that the vocabulary has a gap: it means a
    #: real person wrote something the router could not act on.
    INBOUND_ROUTED = "InboundRouted"
    INBOUND_UNROUTED = "InboundUnrouted"
    WEBHOOK_REJECTED = "WebhookRejected"
    WEBHOOK_SIGNATURE_FAILURE = "WebhookSignatureFailure"
    WEBHOOK_DUPLICATE = "WebhookDuplicate"

    TURN_PROCESSED = "TurnProcessed"
    TURN_LATENCY = "TurnLatencyMs"
    STATE_TRANSITION = "StateTransition"
    ILLEGAL_TRANSITION = "IllegalTransitionRejected"

    PROVIDER_LATENCY = "ProviderLatencyMs"
    PROVIDER_ERROR = "ProviderError"
    CIRCUIT_OPENED = "CircuitOpened"
    RATE_LIMITED = "RateLimited"

    LLM_CALL = "LlmCall"
    LLM_INPUT_TOKENS = "LlmInputTokens"
    LLM_OUTPUT_TOKENS = "LlmOutputTokens"
    LLM_ESTIMATED_COST_MICROS = "LlmEstimatedCostMicros"
    LLM_BUDGET_EXHAUSTED = "LlmBudgetExhausted"

    SLOT_HOLD_CREATED = "SlotHoldCreated"
    SLOT_HOLD_CONFLICT = "SlotHoldConflict"
    DEMO_SCHEDULED = "DemoScheduled"
    DEMO_RESCHEDULED = "DemoRescheduled"
    DEMO_CANCELLED = "DemoCancelled"
    MEET_CREATION_FAILURE = "MeetCreationFailure"

    REMINDER_SENT = "ReminderSent"
    REMINDER_SUPPRESSED = "ReminderSuppressed"
    REMINDER_DELIVERY_FAILURE = "ReminderDeliveryFailure"
    STUDENT_NO_SHOW = "StudentNoShow"
    TUTOR_NO_SHOW = "TutorNoShow"

    OBJECTIONS_EXTRACTED = "ObjectionsExtracted"
    FORECAST_SCORED = "ForecastScored"
    FORECAST_CALIBRATION_ERROR = "ForecastCalibrationError"
    FORECAST_DRIFT = "ForecastDrift"

    DISCOUNT_APPROVED = "DiscountApproved"
    DISCOUNT_ESCALATED = "DiscountEscalated"
    DISCOUNT_DENIED = "DiscountDenied"

    PAYMENT_REQUESTED = "PaymentRequested"
    PAYMENT_VERIFIED = "PaymentVerified"
    PAYMENT_MISMATCH = "PaymentMismatch"
    ACTIVATION_FAILURE = "ActivationFailure"

    HANDOFF_REQUESTED = "HandoffRequested"
    HANDOFF_FAILED = "HandoffFailed"
    HUMAN_HANDOFF = "HumanHandoff"

    OUTBOX_PUBLISHED = "OutboxPublished"
    OUTBOX_FAILED = "OutboxFailed"
    UNDERPERFORMANCE_ALERT = "UnderperformanceAlert"


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: Metric
    value: float
    unit: Unit = Unit.COUNT
    dimensions: tuple[tuple[str, str], ...] = ()


class EmfEmitter:
    """Writes EMF documents to stdout. One line, one metric family."""

    def __init__(self, *, namespace: str = NAMESPACE, stream: TextIO | None = None) -> None:
        self._namespace = namespace
        self._stream = stream or sys.stdout

    def emit(
        self,
        metric: Metric,
        value: float = 1.0,
        *,
        unit: Unit = Unit.COUNT,
        **dimensions: str,
    ) -> None:
        assert_label_safe(dimensions)
        if len(dimensions) > MAX_DIMENSIONS:
            raise ValueError(f"{len(dimensions)} dimensions exceeds CloudWatch's {MAX_DIMENSIONS}")
        document = {
            "_aws": {
                "Timestamp": _now_millis(),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self._namespace,
                        "Dimensions": [sorted(dimensions)] if dimensions else [[]],
                        "Metrics": [{"Name": metric.value, "Unit": unit.value}],
                    }
                ],
            },
            metric.value: value,
            **dimensions,
        }
        print(json.dumps(document, separators=(",", ":")), file=self._stream)


class NullEmitter:
    """No-op for tests and the CLI, so neither pollutes stdout with EMF."""

    def __init__(self) -> None:
        self.emitted: list[MetricPoint] = []

    def emit(
        self,
        metric: Metric,
        value: float = 1.0,
        *,
        unit: Unit = Unit.COUNT,
        **dimensions: str,
    ) -> None:
        assert_label_safe(dimensions)
        self.emitted.append(MetricPoint(metric, value, unit, tuple(sorted(dimensions.items()))))

    def count(self, metric: Metric) -> int:
        return sum(1 for point in self.emitted if point.name is metric)


def _now_millis() -> int:
    import time

    return int(time.time() * 1000)


_emitter: EmfEmitter | NullEmitter = NullEmitter()


def configure(*, enabled: bool) -> None:
    global _emitter
    _emitter = EmfEmitter() if enabled else NullEmitter()


def emit(metric: Metric, value: float = 1.0, *, unit: Unit = Unit.COUNT, **dimensions: str) -> None:
    _emitter.emit(metric, value, unit=unit, **dimensions)


def current_emitter() -> EmfEmitter | NullEmitter:
    return _emitter
